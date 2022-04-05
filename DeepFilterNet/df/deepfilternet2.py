from functools import partial
from typing import Final, List, Optional, Tuple

import torch
from icecream import ic  # noqa
from loguru import logger
from torch import Tensor, nn

from df.config import Csv, DfParams, config
from df.modules import (
    Conv2dNormAct,
    ConvTranspose2dNormAct,
    DfOp,
    GroupedGRU,
    GroupedLinear,
    GroupedLinearEinsum,
    Mask,
    SqueezedGRU,
    erb_fb,
    get_device,
)
from df.multistagenet import MagCompression
from libdf import DF


class ModelParams(DfParams):
    section = "deepfilternet"

    def __init__(self):
        super().__init__()
        self.df_order: int = config("DF_ORDER", cast=int, default=5, section=self.section)
        self.df_lookahead: int = config("DF_LOOKAHEAD", cast=int, default=0, section=self.section)
        self.conv_lookahead: int = config(
            "CONV_LOOKAHEAD", cast=int, default=0, section=self.section
        )
        self.conv_ch: int = config("CONV_CH", cast=int, default=16, section=self.section)
        self.conv_depthwise: bool = config(
            "CONV_DEPTHWISE", cast=bool, default=True, section=self.section
        )
        self.convt_depthwise: bool = config(
            "CONVT_DEPTHWISE", cast=bool, default=True, section=self.section
        )
        self.conv_kernel: List[int] = config(
            "CONV_KERNEL", cast=Csv(int), default=(1, 3), section=self.section  # type: ignore
        )
        self.emb_hidden_dim: int = config(
            "EMB_HIDDEN_DIM", cast=int, default=256, section=self.section
        )
        self.emb_num_layers: int = config(
            "EMB_NUM_LAYERS", cast=int, default=2, section=self.section
        )
        self.df_hidden_dim: int = config(
            "DF_HIDDEN_DIM", cast=int, default=256, section=self.section
        )
        self.df_gru_skip: str = config("DF_GRU_SKIP", default="none", section=self.section)
        self.df_output_layer: str = config(
            "DF_OUTPUT_LAYER", default="linear", section=self.section
        )
        self.df_pathway_kernel_size_t: int = config(
            "DF_PATHWAY_KERNEL_SIZE_T", cast=int, default=1, section=self.section
        )
        self.df_num_layers: int = config("DF_NUM_LAYERS", cast=int, default=3, section=self.section)
        self.df_n_iter: int = config("DF_N_ITER", cast=int, default=2, section=self.section)
        self.gru_type: str = config("GRU_TYPE", default="grouped", section=self.section)
        self.gru_groups: int = config("GRU_GROUPS", cast=int, default=1, section=self.section)
        self.lin_groups: int = config("LINEAR_GROUPS", cast=int, default=1, section=self.section)
        self.group_shuffle: bool = config(
            "GROUP_SHUFFLE", cast=bool, default=True, section=self.section
        )
        self.dfop_method: str = config(
            "DFOP_METHOD", cast=str, default="real_unfold", section=self.section
        )
        self.mask_pf: bool = config("MASK_PF", cast=bool, default=False, section=self.section)


def init_model(df_state: Optional[DF] = None, run_df: bool = True, train_mask: bool = True):
    p = ModelParams()
    if df_state is None:
        df_state = DF(sr=p.sr, fft_size=p.fft_size, hop_size=p.hop_size, nb_bands=p.nb_erb)
    erb = erb_fb(df_state.erb_widths(), p.sr, inverse=False)
    erb_inverse = erb_fb(df_state.erb_widths(), p.sr, inverse=True)
    model = DfNet(erb, erb_inverse, run_df, train_mask)
    return model.to(device=get_device())


class Encoder(nn.Module):
    def __init__(self):
        super().__init__()
        p = ModelParams()
        assert p.nb_erb % 4 == 0, "erb_bins should be divisible by 4"

        self.erb_conv0 = Conv2dNormAct(1, p.conv_ch, kernel_size=(3, 3), bias=False, separable=True)
        conv_layer = partial(
            Conv2dNormAct,
            in_ch=p.conv_ch,
            out_ch=p.conv_ch,
            kernel_size=p.conv_kernel,
            bias=False,
            separable=True,
        )
        self.erb_conv1 = conv_layer(fstride=2)
        self.erb_conv2 = conv_layer(fstride=2)
        self.erb_conv3 = conv_layer(fstride=1)
        self.df_conv0 = Conv2dNormAct(2, p.conv_ch, kernel_size=(3, 3), bias=False, separable=True)
        self.df_conv1 = conv_layer(fstride=2)
        self.erb_bins = p.nb_erb
        self.emb_in_dim = p.conv_ch * p.nb_erb // 4
        self.emb_out_dim = p.emb_hidden_dim
        if p.gru_type == "grouped":
            self.df_fc_emb = GroupedLinear(
                p.conv_ch * p.nb_df // 2, self.emb_in_dim, groups=p.lin_groups
            )
        else:
            self.emb_dim = p.emb_hidden_dim
            self.df_fc_emb = GroupedLinearEinsum(
                p.conv_ch * p.nb_df // 2, self.emb_in_dim, groups=p.lin_groups
            )
        self.emb_out_dim = p.emb_hidden_dim
        self.emb_n_layers = p.emb_num_layers
        assert p.gru_type in ("grouped", "squeeze"), f"But got {p.gru_type}"
        if p.gru_type == "grouped":
            self.emb_gru = GroupedGRU(
                self.emb_in_dim,
                self.emb_out_dim,
                num_layers=1,
                batch_first=True,
                groups=p.gru_groups,
                shuffle=p.group_shuffle,
                add_outputs=True,
            )
        else:
            self.emb_gru = SqueezedGRU(
                self.emb_in_dim, self.emb_out_dim, num_layers=1, batch_first=True
            )
        self.lsnr_fc = nn.Sequential(nn.Linear(self.emb_out_dim, 1), nn.Sigmoid())
        self.lsnr_scale = p.lsnr_max - p.lsnr_min
        self.lsnr_offset = p.lsnr_min

    def forward(
        self, feat_erb: Tensor, feat_spec: Tensor
    ) -> Tuple[Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor]:
        # Encodes erb; erb should be in dB scale + normalized; Fe are number of erb bands.
        # erb: [B, 1, T, Fe]
        # spec: [B, 2, T, Fc]
        b, _, t, _ = feat_erb.shape
        e0 = self.erb_conv0(feat_erb)  # [B, C, T, F]
        e1 = self.erb_conv1(e0)  # [B, C*2, T, F/2]
        e2 = self.erb_conv2(e1)  # [B, C*4, T, F/4]
        e3 = self.erb_conv3(e2)  # [B, C*4, T, F/4]
        c0 = self.df_conv0(feat_spec)  # [B, C, T, Fc]
        c1 = self.df_conv1(c0)  # [B, C*2, T, Fc]
        cemb = c1.permute(0, 2, 3, 1).flatten(2)  # [B, T, -1]
        cemb = self.df_fc_emb(cemb)  # [T, B, C * F/4]
        emb = e3.permute(0, 2, 3, 1).flatten(2)  # [B, T, C * F/4]
        emb = emb + cemb
        emb, _ = self.emb_gru(emb)  # [B, T, -1]
        lsnr = self.lsnr_fc(emb) * self.lsnr_scale + self.lsnr_offset
        return e0, e1, e2, e3, emb, c0, lsnr


class ErbDecoder(nn.Module):
    def __init__(self):
        super().__init__()
        p = ModelParams()
        assert p.nb_erb % 8 == 0, "erb_bins should be divisible by 8"

        self.emb_out_dim = p.emb_hidden_dim

        if p.gru_type == "grouped":
            self.emb_gru = GroupedGRU(
                p.conv_ch * p.nb_erb // 4,  # For compat
                self.emb_out_dim,
                num_layers=p.emb_num_layers - 1,
                batch_first=True,
                groups=p.gru_groups,
                shuffle=p.group_shuffle,
                add_outputs=True,
            )
        else:
            self.emb_gru = SqueezedGRU(
                self.emb_out_dim,
                self.emb_out_dim,
                num_layers=p.emb_num_layers - 1,
                batch_first=True,
                gru_skip_op=nn.Identity,
            )
        # SqueezedGRU uses GroupedLinearEinsum, so let's use it here as well
        if p.gru_type:
            fc_emb = GroupedLinear(
                p.emb_hidden_dim,
                p.conv_ch * p.nb_erb // 4,
                groups=p.lin_groups,
                shuffle=p.group_shuffle,
            )
        else:
            fc_emb = GroupedLinearEinsum(
                p.emb_hidden_dim, p.conv_ch * p.nb_erb // 4, groups=p.lin_groups
            )
        self.fc_emb = nn.Sequential(fc_emb, nn.ReLU(inplace=True))
        tconv_layer = partial(
            ConvTranspose2dNormAct,
            kernel_size=p.conv_kernel,
            bias=False,
            separable=True,
        )
        conv_layer = partial(
            Conv2dNormAct,
            bias=False,
            separable=True,
        )
        # convt: TransposedConvolution, convp: Pathway (encoder to decoder) convolutions
        self.conv3p = conv_layer(p.conv_ch, p.conv_ch, kernel_size=1)
        self.convt3 = conv_layer(p.conv_ch, p.conv_ch, kernel_size=p.conv_kernel)
        self.conv2p = conv_layer(p.conv_ch, p.conv_ch, kernel_size=1)
        self.convt2 = tconv_layer(p.conv_ch, p.conv_ch, fstride=2)
        self.conv1p = conv_layer(p.conv_ch, p.conv_ch, kernel_size=1)
        self.convt1 = tconv_layer(p.conv_ch, p.conv_ch, fstride=2)
        self.conv0p = conv_layer(p.conv_ch, p.conv_ch, kernel_size=1)
        self.conv0_out = conv_layer(
            p.conv_ch, 1, kernel_size=p.conv_kernel, activation_layer=nn.Sigmoid
        )

    def forward(self, emb, e3, e2, e1, e0) -> Tensor:
        # Estimates erb mask
        b, _, t, f8 = e3.shape
        emb, _ = self.emb_gru(emb)
        emb = self.fc_emb(emb)
        emb = emb.view(b, t, f8, -1).permute(0, 3, 1, 2)  # [B, C*8, T, F/8]
        e3 = self.convt3(self.conv3p(e3) + emb)  # [B, C*4, T, F/4]
        e2 = self.convt2(self.conv2p(e2) + e3)  # [B, C*2, T, F/2]
        e1 = self.convt1(self.conv1p(e1) + e2)  # [B, C, T, F]
        m = self.conv0_out(self.conv0p(e0) + e1)  # [B, 1, T, F]
        return m


class DfDecoder(nn.Module):
    def __init__(self):
        super().__init__()
        p = ModelParams()
        layer_width = p.conv_ch
        self.emb_dim = p.emb_hidden_dim

        self.df_n_hidden = p.df_hidden_dim
        self.df_n_layers = p.df_num_layers
        self.df_order = p.df_order
        self.df_bins = p.nb_df
        self.df_lookahead = p.df_lookahead
        self.gru_groups = p.gru_groups

        conv_layer = partial(Conv2dNormAct, separable=True, bias=False)
        kt = p.df_pathway_kernel_size_t
        self.df_convp = conv_layer(layer_width, self.df_order * 2, fstride=1, kernel_size=(kt, 1))
        if p.gru_type == "grouped":
            self.df_gru = GroupedGRU(
                p.emb_hidden_dim,
                p.df_hidden_dim,
                num_layers=self.df_n_layers,
                batch_first=True,
                groups=p.gru_groups,
                shuffle=p.group_shuffle,
                add_outputs=True,
            )
        else:
            self.df_gru = SqueezedGRU(
                p.emb_hidden_dim,
                p.df_hidden_dim,
                num_layers=self.df_n_layers,
                batch_first=True,
                gru_skip_op=nn.Identity,
            )
        p.df_gru_skip = p.df_gru_skip.lower()
        assert p.df_gru_skip in ("none", "identity", "groupedlinear")
        self.df_skip: Optional[nn.Module]
        if p.df_gru_skip == "none":
            self.df_skip = None
        elif p.df_gru_skip == "identity":
            assert p.emb_hidden_dim == p.df_hidden_dim, "Dimensions do not match"
            self.df_skip = nn.Identity()
        elif p.df_gru_skip == "groupedlinear":
            self.df_skip = GroupedLinearEinsum(p.emb_hidden_dim, p.df_hidden_dim, groups=p.lin_groups)
        else:
            raise NotImplementedError()
        assert p.df_output_layer in ("linear", "groupedlinear")
        self.df_out: nn.Module
        out_dim = self.df_bins * self.df_order * 2
        if p.df_output_layer == "linear":
            df_out = nn.Linear(self.df_n_hidden, out_dim)
        elif p.df_output_layer == "groupedlinear":
            df_out = GroupedLinearEinsum(self.df_n_hidden, out_dim, groups=p.lin_groups)
        else:
            raise NotImplementedError
        self.df_out = nn.Sequential(df_out, nn.Tanh())
        self.df_fc_a = nn.Sequential(nn.Linear(self.df_n_hidden, 1), nn.Sigmoid())

    def forward(self, emb: Tensor, c0: Tensor) -> Tuple[Tensor, Tensor]:
        b, t, _ = emb.shape
        c, _ = self.df_gru(emb)  # [B, T, H], H: df_n_hidden
        if self.df_skip is not None:
            c += self.df_skip(emb)
        c0 = self.df_convp(c0).permute(0, 2, 3, 1)  # [B, T, F, O*2], channels_last
        alpha = self.df_fc_a(c)  # [B, T, 1]
        c = self.df_out(c)  # [B, T, F*O*2], O: df_order
        c = c.view(b, t, self.df_bins, self.df_order * 2)  # [B, T, F, O*2]
        c = c.add(c0).view(b, t, self.df_order, 2, self.df_bins).transpose(3, 4)  # [B, T, O, F, 2]
        return c, alpha


class DfDecoderLinear(nn.Module):
    # For compat
    def __init__(self):
        super().__init__()
        p = ModelParams()
        layer_width = p.conv_ch
        self.emb_dim = p.emb_hidden_dim

        self.df_n_hidden = p.df_hidden_dim
        self.df_n_layers = p.df_num_layers
        self.df_order = p.df_order
        self.df_bins = p.nb_df
        self.df_lookahead = p.df_lookahead
        self.gru_groups = p.gru_groups

        conv_layer = partial(Conv2dNormAct, separable=True, bias=False)
        kt = p.df_pathway_kernel_size_t
        self.df_convp = conv_layer(layer_width, self.df_order * 2, fstride=1, kernel_size=(kt, 1))
        self.df_gru = GroupedGRU(
            p.emb_hidden_dim,
            self.df_n_hidden,
            num_layers=self.df_n_layers,
            batch_first=True,
            groups=p.gru_groups,
            shuffle=p.group_shuffle,
            add_outputs=True,
        )
        assert p.df_output_layer == "linear"
        self.df_fc_out = nn.Sequential(
            nn.Linear(self.df_n_hidden, self.df_bins * self.df_order * 2), nn.Tanh()
        )
        self.df_fc_a = nn.Sequential(nn.Linear(self.df_n_hidden, 1), nn.Sigmoid())

    def forward(self, emb: Tensor, c0: Tensor) -> Tuple[Tensor, Tensor]:
        b, t, _ = emb.shape
        c, _ = self.df_gru(emb)  # [B, T, H], H: df_n_hidden
        c0 = self.df_convp(c0).transpose(1, 2)  # [B, T, O*2, F], channels_last
        alpha = self.df_fc_a(c)  # [B, T, 1]
        c = self.df_fc_out(c)  # [B, T, F*O*2], O: df_order
        c = c.view(b, t, self.df_order * 2, self.df_bins)  # [B, T, O*2, F]
        c = c.add(c0).view(b, t, self.df_order, 2, self.df_bins).transpose(3, 4)  # [B, T, O, F, 2]
        return c, alpha


class DfNet(nn.Module):
    run_df: Final[bool]

    def __init__(
        self,
        erb_fb: Tensor,
        erb_inv_fb: Tensor,
        run_df: bool = True,
        train_mask: bool = True,
    ):
        super().__init__()
        p = ModelParams()
        layer_width = p.conv_ch
        assert p.nb_erb % 8 == 0, "erb_bins should be divisible by 8"
        self.lookahead: int = p.conv_lookahead
        self.freq_bins: int = p.fft_size // 2 + 1
        self.emb_dim: int = layer_width * p.nb_erb
        self.erb_bins: int = p.nb_erb
        self.erb_comp = MagCompression(p.nb_erb)
        if p.conv_lookahead > 0:
            pad = (0, 0, -p.conv_lookahead, p.conv_lookahead)
            self.pad = nn.ConstantPad2d(pad, 0.0)
        else:
            self.pad = nn.Identity()
        self.register_buffer("erb_fb", erb_fb, persistent=False)
        self.enc = Encoder()
        self.erb_dec = ErbDecoder()
        self.mask = Mask(erb_inv_fb, post_filter=p.mask_pf)

        self.df_order = p.df_order
        self.df_bins = p.nb_df
        self.df_lookahead = p.df_lookahead
        if p.df_output_layer == "linear":
            self.df_dec = DfDecoderLinear()
        else:
            self.df_dec = DfDecoder()
        self.df_op = torch.jit.script(
            DfOp(
                p.nb_df,
                p.df_order,
                p.df_lookahead,
                freq_bins=self.freq_bins,
                method=p.dfop_method,
            )
        )

        self.run_df = run_df
        if not run_df:
            logger.warning("Runing without DF")
        self.train_mask = train_mask
        self.df_iter = p.df_n_iter

    def forward(
        self,
        spec: Tensor,
        feat_erb: Tensor,
        feat_spec: Tensor,  # Not used, take spec modified by mask instead
    ) -> Tuple[Tensor, Tensor, Tensor, Tensor]:
        feat_spec = feat_spec.squeeze(1).permute(0, 3, 1, 2)

        feat_erb = self.pad(feat_erb)
        feat_spec = self.pad(feat_spec)
        e0, e1, e2, e3, emb, c0, lsnr = self.enc(feat_erb, feat_spec)
        m = self.erb_dec(emb, e3, e2, e1, e0)

        spec = self.mask(spec, m)
        df_coefs, df_alpha = self.df_dec(emb, c0)

        for _ in range(self.df_iter):
            spec = self.df_op(spec, df_coefs, df_alpha)
        return spec, m, lsnr, df_alpha
