import src.context_cache as ctx

from .transformer import *
from src.models.mem_transformer import *
from src.modules.transformer_xl_utils.parameter_init import weights_init

class D2D(NMTModel):
    def __init__(
            self, n_src_vocab, n_tgt_vocab, n_layers=6, n_head=8,
            d_word_vec=512, d_model=512, d_inner_hid=1024, dim_per_head=None,
            dropout=0.1, proj_share_weight=True, **kwargs):

        super(D2D, self).__init__()

        self.encoder = Encoder(
            n_src_vocab, n_layers=n_layers, n_head=n_head,
            d_word_vec=d_word_vec, d_model=d_model,
            d_inner_hid=d_inner_hid, dropout=dropout, dim_per_head=dim_per_head)

        tgt_len, mem_len, ext_len = 4, 4, 0

        self.decoder = MemTransformerLM(n_token=n_tgt_vocab, n_layer=n_layers, n_head=n_head,
                                        d_model=d_model, d_head=dim_per_head,
                                        dropout=dropout, dropatt=dropout,
                                        d_inner=d_inner_hid, attn_type=0,
                                        tgt_len=tgt_len, mem_len=mem_len, ext_len=ext_len,
                                        pre_lnorm=True
                                        )
        self.decoder.apply(weights_init)
        self.decoder.word_emb.apply(weights_init)
        # self.decoder = Decoder(
        #     n_tgt_vocab, n_layers=n_layers, n_head=n_head,
        #     d_word_vec=d_word_vec, d_model=d_model,
        #     d_inner_hid=d_inner_hid, dropout=dropout, dim_per_head=dim_per_head)

        # model = MemTransformerLM(args.n_token, args.n_layer, args.n_head,
        #                          args.d_model, args.d_head, args.d_inner, args.dropout,
        #                          dropatt=args.dropout, tie_weight=True,
        #                          d_embed=d_embed, div_val=div_val,
        #                          tie_projs=tie_projs, pre_lnorm=True,
        #                          tgt_len=tgt_len, ext_len=ext_len, mem_len=mem_len,
        #                          cutoffs=cutoffs, attn_type=0).to(device)

        self.dropout = nn.Dropout(dropout)

        assert d_model == d_word_vec, \
            'To facilitate the residual connections, \
             the dimensions of all module output shall be the same.'

        if proj_share_weight:
            self.generator = Generator(n_words=n_tgt_vocab,
                                       hidden_size=d_word_vec,
                                       shared_weight=self.decoder.word_emb.emb_layers[0].weight,
                                       padding_idx=PAD)

        else:
            self.generator = Generator(n_words=n_tgt_vocab, hidden_size=d_word_vec, padding_idx=PAD)

    def forward(self, src_seq, tgt_seq, log_probs=True):
        enc_output, enc_mask = self.encoder(src_seq)
        dec_inp = tgt_seq

        dec_inp_T = dec_inp.transpose(0, 1)
        enc_out_T = enc_output.transpose(0, 1)
        enc_mask_T = enc_mask.transpose(0, 1)

        dec_pred_T, ctx.memory_cache = self.decoder(dec_inp_T, enc_out_T, enc_mask_T, *ctx.memory_cache)
        dec_pred =  dec_pred_T.transpose(0, 1).contiguous()

        return self.generator(dec_pred, log_probs=log_probs)

    def encode(self, src_seq):

        ctx, ctx_mask = self.encoder(src_seq)
        return {"ctx": ctx, "ctx_mask": ctx_mask}

    def decode(self, tgt_seq, dec_states, log_probs=True):

        enc_output = dec_states["ctx"]
        enc_output_mask = dec_states['ctx_mask']
        enc_attn_caches = dec_states['enc_attn_caches']
        slf_attn_caches = dec_states['slf_attn_caches']

        dec_inp = tgt_seq
        dec_inp_T = dec_inp.transpose(0, 1).contiguous()
        enc_out_T = enc_output.transpose(0, 1).contiguous()

        # dec_output, slf_attn_caches, enc_attn_caches = self.decoder(tgt_seq, )
        dec_pred_T, ctx.memory_cache = self.decoder(dec_inp_T, enc_out_T, *ctx.memory_cache)
        dec_pred = dec_pred_T.transpose(0, 1).contiguous()

        next_scores = self.generator(dec_pred[:, -1].contiguous(), log_probs=log_probs)

        dec_states['enc_attn_caches'] = enc_attn_caches
        dec_states['slf_attn_caches'] = slf_attn_caches

        return next_scores, dec_states

    def init_decoder(self, enc_outputs, expand_size=1):

        ctx = enc_outputs['ctx']

        ctx_mask = enc_outputs['ctx_mask']

        if expand_size > 1:
            ctx = tile_batch(ctx, multiplier=expand_size)
            ctx_mask = tile_batch(ctx_mask, multiplier=expand_size)

        return {
            "ctx": ctx,
            "ctx_mask": ctx_mask,
            "enc_attn_caches": None,
            "slf_attn_caches": None
        }

    def reorder_dec_states(self, dec_states, new_beam_indices, beam_size):

        slf_attn_caches = dec_states['slf_attn_caches']

        batch_size = slf_attn_caches[0][0].size(0) // beam_size

        n_head = self.decoder.n_head
        dim_per_head = self.decoder.d_head

        slf_attn_caches = nest.map_structure(
            lambda t: tensor_gather_helper(gather_indices=new_beam_indices,
                                           gather_from=t,
                                           batch_size=batch_size,
                                           beam_size=beam_size,
                                           gather_shape=[batch_size * beam_size, n_head, -1, dim_per_head]),
            slf_attn_caches)

        dec_states['slf_attn_caches'] = slf_attn_caches

        return dec_states