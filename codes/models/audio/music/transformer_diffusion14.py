import itertools

import torch
import torch.nn as nn
import torch.nn.functional as F

from models.arch_util import AttentionBlock, TimestepEmbedSequential, build_local_attention_mask
from models.audio.music.encoders import ResEncoder16x
from models.diffusion.nn import timestep_embedding, normalization, zero_module, conv_nd, linear
from models.diffusion.unet_diffusion import TimestepBlock
from trainer.networks import register_model
from utils.util import checkpoint, print_network


class SubBlock(nn.Module):
    def __init__(self, inp_dim, contraction_dim, blk_dim, heads, dropout, enable_attention_masking=False):
        super().__init__()
        self.enable_attention_masking = enable_attention_masking
        self.dropout = nn.Dropout(p=dropout)
        self.blk_emb_proj = nn.Conv1d(blk_dim, inp_dim, 1)
        self.attn = AttentionBlock(inp_dim, out_channels=contraction_dim, num_heads=heads)
        self.attnorm = nn.GroupNorm(8, contraction_dim)
        self.ff = nn.Conv1d(inp_dim+contraction_dim, contraction_dim, kernel_size=3, padding=1)
        self.ffnorm = nn.GroupNorm(8, contraction_dim)
        if self.enable_attention_masking:
            # All regions can attend to the first token, which will be the timestep embedding. Hence, fixed_region.
            self.mask = build_local_attention_mask(n=4000, l=48, fixed_region=1)
            self.mask_initialized = False
        else:
            self.mask = None

    def forward(self, x, blk_emb):
        if self.mask is not None and not self.mask_initialized:
            self.mask = self.mask.to(x.device)
            self.mask_initialized = True
        blk_enc = self.blk_emb_proj(blk_emb)
        ah = self.dropout(self.attn(torch.cat([blk_enc, x], dim=-1), mask=self.mask))
        ah = ah[:,:,blk_emb.shape[-1]:]  # Strip off the blk_emb and re-align with x.
        ah = F.gelu(self.attnorm(ah))
        h = torch.cat([ah, x], dim=1)
        hf = self.dropout(checkpoint(self.ff, h))
        hf = F.gelu(self.ffnorm(hf))
        h = torch.cat([h, hf], dim=1)
        return h


class ConcatAttentionBlock(TimestepBlock):
    def __init__(self, trunk_dim, contraction_dim, heads, dropout, enable_attention_masking=False):
        super().__init__()
        self.prenorm = nn.GroupNorm(8, trunk_dim)
        self.block1 = SubBlock(trunk_dim, contraction_dim, trunk_dim, heads, dropout,
                               enable_attention_masking=enable_attention_masking)
        self.block2 = SubBlock(trunk_dim+contraction_dim*2, contraction_dim, trunk_dim, heads, dropout,
                               enable_attention_masking=enable_attention_masking)
        self.out = nn.Conv1d(contraction_dim*4, trunk_dim, kernel_size=1, bias=False)
        self.out.weight.data.zero_()

    def forward(self, x, blk_emb):
        h = self.prenorm(x)
        h = self.block1(h, blk_emb)
        h = self.block2(h, blk_emb)
        h = self.out(h[:,x.shape[1]:])
        return h + x


class TransformerDiffusion(nn.Module):
    """
    A diffusion model composed entirely of stacks of transformer layers. Why would you do it any other way?
    """
    def __init__(
            self,
            time_embed_dim=256,
            model_channels=1024,
            contraction_dim=256,
            num_layers=8,
            in_channels=256,
            input_vec_dim=1024,
            out_channels=512,  # mean and variance
            num_heads=4,
            dropout=0,
            use_corner_alignment=False,  # This is an interpolation parameter only provided for backwards compatibility. ALL NEW TRAINS SHOULD SET THIS TO TRUE.
            use_fp16=False,
            new_code_expansion=False,
            # Parameters for regularization.
            unconditioned_percentage=.1,  # This implements a mechanism similar to what is used in classifier-free training.
            # Parameters for re-training head
            freeze_except_code_converters=False,
    ):
        super().__init__()

        self.in_channels = in_channels
        self.model_channels = model_channels
        self.time_embed_dim = time_embed_dim
        self.out_channels = out_channels
        self.dropout = dropout
        self.unconditioned_percentage = unconditioned_percentage
        self.enable_fp16 = use_fp16
        self.new_code_expansion = new_code_expansion
        self.use_corner_alignment = use_corner_alignment
        self.inp_block = conv_nd(1, in_channels, model_channels, 3, 1, 1)

        self.time_embed = nn.Sequential(
            linear(time_embed_dim, time_embed_dim),
            nn.SiLU(),
            linear(time_embed_dim, model_channels),
        )

        self.input_converter = nn.Conv1d(input_vec_dim, model_channels, 1)
        self.unconditioned_embedding = nn.Parameter(torch.randn(1,model_channels,1))
        self.intg = nn.Conv1d(model_channels*2, model_channels, 1)
        self.layers = TimestepEmbedSequential(*[ConcatAttentionBlock(model_channels, contraction_dim, num_heads, dropout, enable_attention_masking=True) for _ in range(num_layers)])

        self.out = nn.Sequential(
            normalization(model_channels),
            nn.SiLU(),
            zero_module(conv_nd(1, model_channels, out_channels, 3, padding=1)),
        )

        if freeze_except_code_converters:
            for p in self.parameters():
                p.DO_NOT_TRAIN = True
                p.requires_grad = False
            for m in [self.code_converter and self.input_converter]:
                for p in m.parameters():
                    del p.DO_NOT_TRAIN
                    p.requires_grad = True

    def get_grad_norm_parameter_groups(self):
        attn1 = list(itertools.chain.from_iterable([lyr.block1.attn.parameters() for lyr in self.layers]))
        attn2 = list(itertools.chain.from_iterable([lyr.block2.attn.parameters() for lyr in self.layers]))
        ff1 = list(itertools.chain.from_iterable([lyr.block1.ff.parameters() for lyr in self.layers]))
        ff2 = list(itertools.chain.from_iterable([lyr.block2.ff.parameters() for lyr in self.layers]))
        blkout_layers = list(itertools.chain.from_iterable([lyr.out.parameters() for lyr in self.layers]))
        groups = {
            'prenorms': list(itertools.chain.from_iterable([lyr.prenorm.parameters() for lyr in self.layers])),
            'blk1_attention_layers': attn1,
            'blk2_attention_layers': attn2,
            'attention_layers': attn1 + attn2,
            'blk1_ff_layers': ff1,
            'blk2_ff_layers': ff2,
            'ff_layers': ff1 + ff2,
            'block_out_layers': blkout_layers,
            'out': list(self.out.parameters()),
            'x_proj': list(self.inp_block.parameters()),
            'layers': list(self.layers.parameters()),
            'time_embed': list(self.time_embed.parameters()),
        }
        return groups

    def forward(self, x, timesteps, prior=None, conditioning_free=False):
        if conditioning_free:
            code_emb = self.unconditioned_embedding.repeat(x.shape[0], 1, x.shape[-1])
        else:
            code_emb = self.input_converter(prior)

            # Mask out the conditioning branch for whole batch elements, implementing something similar to classifier-free guidance.
            if self.training and self.unconditioned_percentage > 0:
                unconditioned_batches = torch.rand((code_emb.shape[0], 1, 1),
                                                   device=code_emb.device) < self.unconditioned_percentage
                code_emb = torch.where(unconditioned_batches, self.unconditioned_embedding.repeat(x.shape[0], 1, 1),
                                       code_emb)

            code_emb = F.interpolate(code_emb, size=x.shape[-1], mode='nearest')

        with torch.autocast(x.device.type, enabled=self.enable_fp16):
            blk_emb = self.time_embed(timestep_embedding(timesteps, self.time_embed_dim)).unsqueeze(-1)
            x = self.inp_block(x)

            x = self.intg(torch.cat([x, code_emb], dim=1))
            for layer in self.layers:
                x = checkpoint(layer, x, blk_emb)

        x = x.float()
        out = self.out(x)

        # Defensively involve probabilistic or possibly unused parameters in loss so we don't get DDP errors.
        unused_params = [self.unconditioned_embedding]
        extraneous_addition = 0
        for p in unused_params:
            extraneous_addition = extraneous_addition + p.mean()
        out = out + extraneous_addition * 0

        return out


class TransformerDiffusionWithCheaterLatent(nn.Module):
    def __init__(self, freeze_encoder_until=None, checkpoint_encoder=True, **kwargs):
        super().__init__()
        self.internal_step = 0
        self.freeze_encoder_until = freeze_encoder_until
        self.diff = TransformerDiffusion(**kwargs)
        self.encoder = ResEncoder16x(256, 1024, 256, checkpointing_enabled=checkpoint_encoder)

    def forward(self, x, timesteps, truth_mel, conditioning_free=False):
        unused_parameters = []
        encoder_grad_enabled = self.freeze_encoder_until is not None and self.internal_step > self.freeze_encoder_until
        if not encoder_grad_enabled:
            unused_parameters.extend(list(self.encoder.parameters()))
        with torch.set_grad_enabled(encoder_grad_enabled):
            proj = self.encoder(truth_mel)

        for p in unused_parameters:
            proj = proj + p.mean() * 0

        diff = self.diff(x, timesteps, prior=proj, conditioning_free=conditioning_free)
        return diff

    def get_debug_values(self, step, __):
        self.internal_step = step
        return {}

    def get_grad_norm_parameter_groups(self):
        groups = self.diff.get_grad_norm_parameter_groups()
        groups['encoder'] = list(self.encoder.parameters())
        return groups

    def before_step(self, step):
        scaled_grad_parameters = list(itertools.chain.from_iterable([lyr.out.parameters() for lyr in self.diff.layers])) + \
                                 list(itertools.chain.from_iterable([lyr.prenorm.parameters() for lyr in self.diff.layers]))
        # Scale back the gradients of the blkout and prenorm layers by a constant factor. These get two orders of magnitudes
        # higher gradients. Ideally we would use parameter groups, but ZeroRedundancyOptimizer makes this trickier than
        # directly fiddling with the gradients.
        for p in scaled_grad_parameters:
            if hasattr(p, 'grad') and p.grad is not None:
                p.grad *= .2


@register_model
def register_transformer_diffusion14(opt_net, opt):
    return TransformerDiffusion(**opt_net['kwargs'])


@register_model
def register_transformer_diffusion_14_with_cheater_latent(opt_net, opt):
    return TransformerDiffusionWithCheaterLatent(**opt_net['kwargs'])


def test_tfd():
    clip = torch.randn(2,256,400)
    ts = torch.LongTensor([600, 600])
    model = TransformerDiffusion(in_channels=256, model_channels=1024, contraction_dim=512,
                                              num_heads=3, input_vec_dim=256, num_layers=12, dropout=.1)
    model(clip, ts, clip)


def test_cheater_model():
    clip = torch.randn(2, 256, 400)
    ts = torch.LongTensor([600, 600])

    # For music:
    model = TransformerDiffusionWithCheaterLatent(in_channels=256, out_channels=512,
                                                    model_channels=1024, contraction_dim=512, num_heads=8,
                                                    input_vec_dim=256, num_layers=16,
                                                    dropout=.1, new_code_expansion=True,
                                              )
    #diff_weights = torch.load('extracted_diff.pth')
    #model.diff.load_state_dict(diff_weights, strict=False)
    #model.encoder.load_state_dict(torch.load('../experiments/music_cheater_encoder_256.pth', map_location=torch.device('cpu')), strict=True)
    #torch.save(model.state_dict(), 'sample.pth')

    print_network(model)
    o = model(clip, ts, clip)
    o = model(clip, ts, clip, conditioning_free=True)
    pg = model.get_grad_norm_parameter_groups()


def extract_cheater_encoder(in_f, out_f):
    p = torch.load(in_f)
    out = {}
    for k, v in p.items():
        if k.startswith('encoder.'):
            out[k] = v
    torch.save(out, out_f)


if __name__ == '__main__':
    #test_local_attention_mask()
    #extract_cheater_encoder('X:\\dlas\\experiments\\train_music_diffusion_tfd_and_cheater\\models\\104500_generator_ema.pth', 'X:\\dlas\\experiments\\tfd12_self_learned_cheater_enc.pth', True)
    test_cheater_model()
    #extract_diff('X:\\dlas\experiments\\train_music_diffusion_tfd_cheater_from_scratch\\models\\56500_generator_ema.pth', 'extracted.pth', remove_head=True)
