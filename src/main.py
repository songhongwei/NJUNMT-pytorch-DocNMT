import os
import random
import time
from copy import deepcopy

import numpy as np
import torch
import yaml
from tensorboardX import SummaryWriter
from tqdm import tqdm

from src.data.data_iterator import DataIterator
from src.data.dataset import TextLineDataset, ZipDataset
from src.data.vocabulary import Vocabulary
from src.decoding import beam_search, ensemble_beam_search
from src.metric.bleu_scorer import SacreBLEUScorer
from src.models import build_model
from src.modules.criterions import NMTCriterion
from src.optim import Optimizer
from src.optim.lr_scheduler import ReduceOnPlateauScheduler, NoamScheduler
from src.utils.common_utils import *
from src.utils.configs import default_configs, pretty_configs
from src.utils.logging import *
from src.utils.moving_average import MovingAverage

import src.context_cache as ctx

BOS = Vocabulary.BOS
EOS = Vocabulary.EOS
PAD = Vocabulary.PAD


def set_seed(seed):
    torch.manual_seed(seed)

    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    random.seed(seed)

    np.random.seed(seed)

    torch.backends.cudnn.deterministic = True


def load_model_parameters(path, map_location="cpu"):
    state_dict = torch.load(path, map_location=map_location)

    if "model" in state_dict:
        return state_dict["model"]
    return state_dict


def split_shard(*inputs, split_size=1):
    if split_size <= 1:
        yield inputs
    else:

        lengths = [len(s) for s in inputs[-1]]  #
        sorted_indices = np.argsort(lengths)

        # sorting inputs

        inputs = [
            [inp[ii] for ii in sorted_indices]
            for inp in inputs
        ]

        # split shards
        total_batch = sorted_indices.shape[0]  # total number of batches

        if split_size >= total_batch:
            yield inputs
        else:
            shard_size = total_batch // split_size

            _indices = list(range(total_batch))[::shard_size] + [total_batch]

            for beg, end in zip(_indices[:-1], _indices[1:]):
                yield (inp[beg:end] for inp in inputs)


def prepare_data(seqs_x, seqs_y=None, cuda=False, batch_first=True):
    """
    Args:
        eval ('bool'): indicator for eval/infer.

    Returns:

    """

    def _np_pad_batch_2D(samples, pad, batch_first=True, cuda=True):

        batch_size = len(samples)

        sizes = [len(s) for s in samples]
        max_size = max(sizes)

        x_np = np.full((batch_size, max_size), fill_value=pad, dtype='int64')

        for ii in range(batch_size):
            x_np[ii, :sizes[ii]] = samples[ii]

        if batch_first is False:
            x_np = np.transpose(x_np, [1, 0])

        x = torch.tensor(x_np)

        if cuda is True:
            x = x.cuda()
        return x

    seqs_x = list(map(lambda s: [BOS] + s + [EOS] if len(s) != 0 else s, seqs_x))
    x = _np_pad_batch_2D(samples=seqs_x, pad=PAD,
                         cuda=cuda, batch_first=batch_first)

    if seqs_y is None:
        return x

    seqs_y = list(map(lambda s: [BOS] + s + [EOS], seqs_y))
    y = _np_pad_batch_2D(seqs_y, pad=PAD,
                         cuda=cuda, batch_first=batch_first)

    return x, y


def prepare_data_doc(seqs_x):
    # seqs_x: 2D list (n_doc, doc_seqs)
    # return : 3D list (n_sent, tensor(n_doc, sent_seqs))
    x_split = tgt_doc_seq_split(seqs_x) # (n_sent, n_doc, sent_seqs)
    x_batch = [prepare_data(x_batch_untensored, cuda=GlobalNames.USE_GPU)
        for x_batch_untensored in x_split] # 

    # 3D list (n_sent, tensor(n_doc, sent_seqs))
    return x_batch


#added by yx 20191108
def src_doc_seq_add_eos_bos(src_seqs: list):
    SEP_id = ctx.vocab_tgt.token2id('<SEP>')

    split_res = []
    seq_no = 0
    for seq in src_seqs:
        last_sep_index = 0
        for i in range(len(seq)):
            if seq[i] == SEP_id:
                sent = seq[last_sep_index:i]  # last start word ~ word before SEP
                if len(split_res) < seq_no + 1:
                    split_res.append([])
                    split_res[seq_no] += sent
                else:
                    split_res[seq_no] += [EOS, BOS] + sent
                last_sep_index = i + 1
        seq_no += 1
    return split_res

def src_doc_sents_map(src_seqs: torch.Tensor):
    sent_mapping = src_seqs.tolist()
    sent_rank = deepcopy(sent_mapping)
    max_n_sents = 0
    for i in range(len(sent_mapping)):
        n_sents = -1
        cur_rank = 0
        for j in range(len(sent_mapping[i])):
            id = sent_mapping[i][j]
            if id == BOS:
                n_sents += 1
                cur_rank = 0
            sent_mapping[i][j] = n_sents
            sent_rank[i][j] = cur_rank
            cur_rank += 1
        if max_n_sents < n_sents:
            max_n_sents = n_sents
    res = torch.tensor(sent_mapping).detach()
    sent_rank = torch.tensor(sent_rank, dtype=torch.float32, device=src_seqs.device)
    if GlobalNames.USE_GPU:
        res = res.cuda()
    return res, sent_rank, max_n_sents

#added by yx 20191107
def tgt_doc_seq_split(tgt_seqs: list):
    split_res = []
    seq_no = 0
    for seq in tgt_seqs:
        last_sep_index = 0
        seq = [BOS]+seq+[EOS]
        for i in range(len(seq)):
            if seq[i] == EOS:
                sent = seq[ 1+last_sep_index : i ]  #word after BOS ~ word before EOS
                # if BOS not in sent and EOS not in sent:
                #     sent = [BOS] + sent + [EOS]
                if len(split_res) < seq_no + 1:
                    split_res.append([])
                split_res[seq_no].append(sent)
                last_sep_index = i+1
        seq_no += 1
    max_n_sents = max( [ len(sents_of_seq) for sents_of_seq in split_res ] )    #get max sents number, ready for padding
    for i in range( len(split_res) ):
        ext_len = max_n_sents - len(split_res[i])
        if ext_len == 0:
            continue
        while ext_len != 0:
            split_res[i] += [ [] ]
            ext_len -= 1
    dec_batch = list(map(list, zip(*split_res)))    #transpose!
    # dec_batch = np.array(split_res).transpose()
    return dec_batch



def compute_forward(model,
                    critic,
                    x_batch,
                    y_batch,
                    seqs_x=None,
                    eval=False,
                    normalization=1.0,
                    norm_by_words=False
                    ):
    """
    :type model: nn.Module

    :type critic: NMTCriterion
    """

    total_loss = 0
    if not eval:
        model.train()
        critic.train()
    else:
        model.eval()
        critic.eval()

    if ctx.GLOBAL_ENCODING:
        sents_mapping, sent_rank, _ = src_doc_sents_map(seqs_x)
        with torch.set_grad_enabled(not eval):
            enc_out, enc_mask = model.encoder(seqs_x, position=sent_rank, segment_ids=sents_mapping)

    if ctx.GLOBAL_CAT:  # extend mapping & mask to 2x because enc_output=[enc, glb_enc]
        sents_mapping = torch.cat([sents_mapping, sents_mapping], dim=1)
        enc_mask = torch.cat([enc_mask, enc_mask], dim=1)

    n_sents = len(x_batch)
    n_docs = x_batch[0].size(0) 

    ctx.memory_cache = tuple()
    ctx.memory_mask = None

    for sents_no, (x_sents, y_sents) in enumerate(zip(x_batch, y_batch)):
    # x_sents, y_sents: tensor(n_doc, n_words)    
        
        
    #     #####show sentence######
    #     tgt_batch_sents = []
    #     for sent in y_sents:
    #         sent = sent.tolist()
    #         tgt_batch_sents.append(ctx.vocab_tgt.ids2sent(sent))
    #     print(tgt_batch_sents)

        n_sents += y_sents.size(0)

        y_inp = y_sents[:, :-1].contiguous()
        y_label = y_sents[:, 1:].contiguous()

        # mask all non-current sentences
        if ctx.GLOBAL_ENCODING:
            is_not_current_sents = sents_mapping.detach().ne(sents_no)
            current_sent_mask = torch.where(is_not_current_sents, is_not_current_sents, enc_mask)
        else:
            # encode current sentence
            with torch.set_grad_enabled(not eval):
                enc_out, current_sent_mask = model.encoder(x_sents)

        if not eval:
            # For training
            with torch.enable_grad():
                log_probs = model.decode_train(y_inp, enc_out, current_sent_mask, log_probs=True)
                loss = critic(inputs=log_probs, labels=y_label, reduce=False, normalization=1)

        else:
            # For compute loss
            with torch.no_grad():
                log_probs = model.decode_train(y_inp, enc_out, current_sent_mask, log_probs=True)
                loss = critic(inputs=log_probs, labels=y_label, normalization=1, reduce=True)

        # @zzx (2019-11-22)： build mem mask for last sentences
        ctx.memory_mask = y_label.eq(PAD).view(-1, y_label.size(-1)).transpose(0, 1)

        if norm_by_words:
            words_norm = y_label.ne(PAD).float().sum(1)
            loss = loss.div(words_norm).sum()
        else:
            loss = loss.sum()
        total_loss += loss
    # end of for-LOOP

    if not eval:
        torch.autograd.backward(total_loss / normalization)
    return total_loss.item()


def loss_validation(model, critic, valid_iterator):
    """
    :type model: Transformer

    :type critic: NMTCriterion

    :type valid_iterator: DataIterator
    """

    n_seqs = 0
    n_sents = 0
    n_tokens = 0.0

    sum_loss = 0
    valid_iter = valid_iterator.build_generator()

    for batch in valid_iter:
        _, seqs_x, seqs_y = batch

        ctx.memory_cache = tuple()  ##flush cache

        n_seqs += len(seqs_x)
        n_tokens += sum(len(s) for s in seqs_y)
        n_sents_of_current_seq = sum( [ seq.count(BOS)+1 for seq in seqs_x ] )
        n_sents += n_sents_of_current_seq
        # x_add_eos_bos = src_doc_seq_add_eos_bos(seqs_x)
        x = prepare_data(seqs_x, cuda=GlobalNames.USE_GPU)
        # y_split = tgt_doc_seq_split(seqs_y)
        # y_dec_batch = [ prepare_data(y_batch_untensored, cuda=GlobalNames.USE_GPU)
        #                for y_batch_untensored in y_split ]
        x_batch, y_batch = prepare_data_doc(seqs_x), prepare_data_doc(seqs_y)

        loss = compute_forward(model=model,
                               critic=critic,
                               seqs_x=x,
                               x_batch=x_batch,
                               y_batch=y_batch,
                               eval=True)

        if np.isnan(loss):
            WARN("NaN detected!")

        sum_loss += float(loss)

    # return float(sum_loss / n_seqs )
    return float(sum_loss / n_sents )


def bleu_validation(uidx,
                    valid_iterator,
                    model,
                    bleu_scorer,
                    vocab_tgt,
                    batch_size,
                    valid_dir="./valid",
                    max_steps=10,
                    beam_size=5,
                    alpha=-1.0
                    ):
    model.eval()
    ctx.IS_INFERRING = True

    numbers = []
    trans_docs = []

    infer_progress_bar = tqdm(total=len(valid_iterator),
                              desc=' - (Infer)  ',
                              unit="sents")

    valid_iter = valid_iterator.build_generator(batch_size=batch_size)

    for batch in valid_iter:

        seq_nums = batch[0]
        numbers += seq_nums

        seqs_x = batch[1]

        infer_progress_bar.update(len(seqs_x))


        # x_add_eos_bos = src_doc_seq_add_eos_bos(seqs_x)
        if ctx.GLOBAL_ENCODING:
            x = prepare_data(seqs_x, cuda=GlobalNames.USE_GPU)
            # sents_mapping, max_n_sents = src_doc_sents_map(x)
            # enc_out, enc_mask = model.encoder(x)
            sents_mapping, sent_rank, _ = src_doc_sents_map(x)
            with torch.set_grad_enabled(False):
                enc_out, enc_mask = model.encoder(x, position=sent_rank, segment_ids=sents_mapping)

        if ctx.GLOBAL_CAT:  # extend mapping & mask to 2x because enc_output=[enc, glb_enc]
            sents_mapping = torch.cat([sents_mapping, sents_mapping], dim=1)
            enc_mask = torch.cat([enc_mask, enc_mask], dim=1)

        x_batch = prepare_data_doc(seqs_x)

        trans_sents2doc = []
        for i in range(len(seq_nums)):
            trans_sents2doc.append( [] )

        ctx.memory_cache = tuple()
        ctx.memory_mask = None

        for sents_no, x_sents in enumerate(x_batch):
            # mask all non-current sentences
            if ctx.GLOBAL_ENCODING:
                is_not_current_sents = sents_mapping.detach().ne(sents_no)
                current_sent_mask = torch.where(is_not_current_sents, is_not_current_sents, enc_mask)
            else:
                # encode current sentence
                with torch.set_grad_enabled(False):
                    enc_out, current_sent_mask = model.encoder(x_sents)

            with torch.no_grad():
                dec_state = {"ctx": enc_out, "ctx_mask": current_sent_mask}
                word_ids = beam_search(nmt_model=model, beam_size=beam_size, max_steps=max_steps, dec_state=dec_state, alpha=alpha)
                # ctx.memory_mask = word_ids.eq(PAD).view(-1, word_ids.size(-1)).transpose(0, 1)

            word_ids = word_ids.cpu().numpy().tolist()

            # Append result
            iter_num = 0

            for sent_t in word_ids:
                sent_t = [[wid for wid in line if wid != PAD] for line in sent_t]
                x_tokens = []

                for wid in sent_t[0]:
                    if wid == EOS:
                        break
                    x_tokens.append(vocab_tgt.id2token(wid))

                if len(x_tokens) > 0:
                    trans_sents2doc[iter_num].append( vocab_tgt.tokenizer.detokenize(x_tokens) )
                # @zzx (2019-11-22): adding eos for empty-results (from all-padded source) 
                # leads to extra translation output. Just skip it
                # else:
                #     trans_sents2doc[iter_num].append( '%s' % vocab_tgt.id2token(EOS) )
                iter_num += 1
        trans_docs.extend(trans_sents2doc)
    
    ctx.IS_INFERRING = False

    origin_order = np.argsort(numbers).tolist()
    trans_docs = [trans_docs[ii] for ii in origin_order]

    trans_sents = []
    for trans_doc in trans_docs:
        for trans_sent in trans_doc:
            trans_sents.append(trans_sent)
    #split doc trans results into sents

    infer_progress_bar.close()

    if not os.path.exists(valid_dir):
        os.mkdir(valid_dir)

    hyp_path = os.path.join(valid_dir, 'trans.iter{0}.txt'.format(uidx))

    with open(hyp_path, 'w') as f:
        for line in trans_sents:
            f.write('%s\n' % line)

    with open(hyp_path) as f:
        bleu_v = bleu_scorer.corpus_bleu(f)

    return bleu_v


def load_pretrained_model(nmt_model, pretrain_path, device, exclude_prefix=None):
    """
    Args:
        nmt_model: model.
        pretrain_path ('str'): path to pretrained model.
        map_dict ('dict'): mapping specific parameter names to those names
            in current model.
        exclude_prefix ('dict'): excluding parameters with specific names
            for pretraining.

    Raises:
        ValueError: Size not match, parameter name not match or others.

    """
    if exclude_prefix is None:
        exclude_prefix = []
    if pretrain_path != "":
        INFO("Loading pretrained model from {}".format(pretrain_path))
        pretrain_params = torch.load(pretrain_path, map_location=device)
        for name, params in pretrain_params.items():
            flag = False
            for pp in exclude_prefix:
                if name.startswith(pp):
                    flag = True
                    break
            if flag:
                continue
            INFO("Loading param: {}...".format(name))
            try:
                nmt_model.load_state_dict({name: params}, strict=False)
            except Exception as e:
                WARN("{}: {}".format(str(Exception), e))

        INFO("Pretrained model loaded.")


def train(FLAGS):
    """
    FLAGS:
        saveto: str
        reload: store_true
        config_path: str
        pretrain_path: str, default=""
        model_name: str
        log_path: str
    """

    # write log of training to file.
    write_log_to_file(os.path.join(FLAGS.log_path, "%s.log" % time.strftime("%Y%m%d-%H%M%S")))

    GlobalNames.USE_GPU = FLAGS.use_gpu

    if GlobalNames.USE_GPU:
        CURRENT_DEVICE = "cpu"
    else:
        CURRENT_DEVICE = "cuda:0"

    config_path = os.path.abspath(FLAGS.config_path)
    with open(config_path.strip()) as f:
        configs = yaml.load(f)

    INFO(pretty_configs(configs))

    # Add default configs
    configs = default_configs(configs)
    data_configs = configs['data_configs']
    model_configs = configs['model_configs']
    optimizer_configs = configs['optimizer_configs']
    training_configs = configs['training_configs']
    ctx.ENABLE_CONTEXT = model_configs['enable_history_context']
    ctx.GLOBAL_ENCODING = model_configs['enable_global_encoding']
    ctx.GLOBAL_CAT = model_configs['global_encoder_cat']

    GlobalNames.SEED = training_configs['seed']

    set_seed(GlobalNames.SEED)

    best_model_prefix = os.path.join(FLAGS.saveto, FLAGS.model_name + GlobalNames.MY_BEST_MODEL_SUFFIX)

    timer = Timer()

    # ================================================================================== #
    # Load Data

    INFO('Loading data...')
    timer.tic()

    # Generate target dictionary
    vocab_src = Vocabulary(**data_configs["vocabularies"][0])
    vocab_tgt = Vocabulary(**data_configs["vocabularies"][1])

    ctx.vocab_tgt = vocab_tgt

    train_batch_size = training_configs["batch_size"] * max(1, training_configs["update_cycle"])
    train_buffer_size = training_configs["buffer_size"] * max(1, training_configs["update_cycle"])

    train_bitext_dataset = ZipDataset(
        TextLineDataset(data_path=data_configs['train_data'][0],
                        vocabulary=vocab_src,
                        max_len=data_configs['max_len'][0],
                        ),
        TextLineDataset(data_path=data_configs['train_data'][1],
                        vocabulary=vocab_tgt,
                        max_len=data_configs['max_len'][1],
                        ),
        shuffle=training_configs['shuffle']
    )

    valid_bitext_dataset = ZipDataset(
        TextLineDataset(data_path=data_configs['valid_data'][0],
                        vocabulary=vocab_src,
                        ),
        TextLineDataset(data_path=data_configs['valid_data'][1],
                        vocabulary=vocab_tgt,
                        )
    )

    training_iterator = DataIterator(dataset=train_bitext_dataset,
                                     batch_size=train_batch_size,
                                     use_bucket=training_configs['use_bucket'],
                                     buffer_size=train_buffer_size,
                                     batching_func=training_configs['batching_key'])

    valid_iterator = DataIterator(dataset=valid_bitext_dataset,
                                  batch_size=training_configs['valid_batch_size'],
                                  use_bucket=True, buffer_size=100000, numbering=True)

    bleu_scorer = SacreBLEUScorer(reference_path=data_configs["bleu_valid_reference"],
                                  num_refs=data_configs["num_refs"],
                                  lang_pair=data_configs["lang_pair"],
                                  sacrebleu_args=training_configs["bleu_valid_configs"]['sacrebleu_args'],
                                  postprocess=training_configs["bleu_valid_configs"]['postprocess']
                                  )

    INFO('Done. Elapsed time {0}'.format(timer.toc()))

    lrate = optimizer_configs['learning_rate']
    is_early_stop = False

    # ================================ Begin ======================================== #
    # Build Model & Optimizer
    # We would do steps below on after another
    #     1. build models & criterion
    #     2. move models & criterion to gpu if needed
    #     3. load pre-trained model if needed
    #     4. build optimizer
    #     5. build learning rate scheduler if needed
    #     6. load checkpoints if needed

    # 0. Initial
    model_collections = Collections()
    checkpoint_saver = Saver(save_prefix="{0}.ckpt".format(os.path.join(FLAGS.saveto, FLAGS.model_name)),
                             num_max_keeping=training_configs['num_kept_checkpoints']
                             )
    best_model_saver = Saver(save_prefix=best_model_prefix, num_max_keeping=training_configs['num_kept_best_model'])

    # 1. Build Model & Criterion
    INFO('Building model...')
    timer.tic()
    nmt_model = build_model(n_src_vocab=vocab_src.max_n_words,
                            n_tgt_vocab=vocab_tgt.max_n_words, **model_configs)
    INFO(nmt_model)

    params_total = sum([p.numel() for n, p in nmt_model.named_parameters()])
    params_with_embedding = sum([p.numel() for n, p in nmt_model.named_parameters() if n.find('embedding') == -1])
    INFO('Total parameters: {}'.format(params_total))
    INFO('Total parameters (excluding word embeddings): {}'.format(params_with_embedding))

    critic = NMTCriterion(label_smoothing=model_configs['label_smoothing'])

    INFO(critic)
    INFO('Done. Elapsed time {0}'.format(timer.toc()))

    # 2. Move to GPU
    if GlobalNames.USE_GPU:
        nmt_model = nmt_model.cuda()
        critic = critic.cuda()

    # 3. Load pretrained model if needed
    load_pretrained_model(nmt_model, FLAGS.pretrain_path, exclude_prefix=None, device=CURRENT_DEVICE)

    # 4. Build optimizer
    INFO('Building Optimizer...')
    optim = Optimizer(name=optimizer_configs['optimizer'],
                      model=nmt_model,
                      lr=lrate,
                      grad_clip=optimizer_configs['grad_clip'],
                      optim_args=optimizer_configs['optimizer_params']
                      )
    # 5. Build scheduler for optimizer if needed
    if optimizer_configs['schedule_method'] is not None:

        if optimizer_configs['schedule_method'] == "loss":

            scheduler = ReduceOnPlateauScheduler(optimizer=optim,
                                                 **optimizer_configs["scheduler_configs"]
                                                 )

        elif optimizer_configs['schedule_method'] == "noam":
            scheduler = NoamScheduler(optimizer=optim, **optimizer_configs['scheduler_configs'])
        else:
            WARN("Unknown scheduler name {0}. Do not use lr_scheduling.".format(optimizer_configs['schedule_method']))
            scheduler = None
    else:
        scheduler = None

    # 6. build moving average

    if training_configs['moving_average_method'] is not None:
        ma = MovingAverage(moving_average_method=training_configs['moving_average_method'],
                           named_params=nmt_model.named_parameters(),
                           alpha=training_configs['moving_average_alpha'])
    else:
        ma = None

    INFO('Done. Elapsed time {0}'.format(timer.toc()))

    # Reload from latest checkpoint
    if FLAGS.reload:
        checkpoint_saver.load_latest(model=nmt_model, optim=optim, lr_scheduler=scheduler,
                                     collections=model_collections, ma=ma)

    # ================================================================================== #
    # Prepare training

    eidx = model_collections.get_collection("eidx", [0])[-1]
    uidx = model_collections.get_collection("uidx", [0])[-1]
    bad_count = model_collections.get_collection("bad_count", [0])[-1]
    oom_count = model_collections.get_collection("oom_count", [0])[-1]

    summary_writer = SummaryWriter(log_dir=FLAGS.log_path)

    cum_samples = 0
    cum_words = 0
    valid_loss = best_valid_loss = float('inf') # Max Float
    valid_bleu = best_valid_bleu = 0 
    saving_files = []

    # Timer for computing speed
    timer_for_speed = Timer()
    timer_for_speed.tic()

    INFO('Begin training...')

    while True:

        summary_writer.add_scalar("Epoch", (eidx + 1), uidx)

        # Build iterator and progress bar
        training_iter = training_iterator.build_generator()
        training_progress_bar = tqdm(desc=' - (Epc {}, Upd {}) '.format(eidx, uidx),
                                     total=len(training_iterator),
                                     unit="sents"
                                     )
        for batch in training_iter:

            # ctx.memory_cache = tuple()  ## flush memory cache

            uidx += 1

            if optimizer_configs["schedule_method"] is not None and optimizer_configs["schedule_method"] != "loss":
                scheduler.step(global_step=uidx)

            seqs_x, seqs_y = batch

            #####show sentence######
            # src_batch_sents = []
            # tgt_batch_sents = []
            # for sent in seqs_x:
            #     src_batch_sents.append(vocab_src.ids2sent(sent))
            # for sent in seqs_y:
            #     tgt_batch_sents.append(vocab_tgt.ids2sent(sent))
            #
            # print(src_batch_sents)
            # print(tgt_batch_sents)

            ######################

            n_samples_t = len(seqs_x)
            n_words_s = sum(len(s) for s in seqs_x)
            n_words_t = sum(len(s) for s in seqs_y)

            cum_samples += n_samples_t
            cum_words += n_words_t


            train_loss = 0.
            optim.zero_grad()
            try:
                # Prepare data
                for seqs_x_t, seqs_y_t in split_shard(seqs_x, seqs_y, split_size=training_configs['update_cycle']):
                    # x, y = prepare_data(seqs_x_t, seqs_y_t, cuda=GlobalNames.USE_GPU)
                    # x_add_eos_bos = src_doc_seq_add_eos_bos(seqs_x_t)
                    x = prepare_data(seqs_x_t, cuda=GlobalNames.USE_GPU)
                    x_batch, y_batch = prepare_data_doc(seqs_x_t), prepare_data_doc(seqs_y_t)

                    loss = compute_forward(model=nmt_model,
                                           critic=critic,
                                           seqs_x=x,
                                           x_batch=x_batch,
                                           y_batch=y_batch,
                                           eval=False,
                                           normalization=n_samples_t*20, # assume that there are 20 sents per doc
                                           norm_by_words=training_configs["norm_by_words"])
                    # total_words = sum( [ batch.size(0)*batch.size(1) for batch in y_batch ] )
                    train_loss += loss / n_words_t
                                     #get avg loss per word
                optim.step()

            except RuntimeError as e:
                if 'out of memory' in str(e):
                    print('| WARNING: ran out of memory, skipping batch')
                    oom_count += 1
                    optim.zero_grad()
                else:
                    raise e

            if ma is not None and eidx >= training_configs['moving_average_start_epoch']:
                ma.step()

            training_progress_bar.update(n_samples_t)
            training_progress_bar.set_description(' - (Epc {}, Upd {}) '.format(eidx, uidx))
            training_progress_bar.set_postfix_str(
                'train: {:.2f}, valid(bst): {:.2f}({:.2f}), BLEU(bst): {:.2f}({:.2f})'
                .format(train_loss, valid_loss, best_valid_loss, valid_bleu, best_valid_bleu))
            summary_writer.add_scalar("train_loss", scalar_value=train_loss, global_step=uidx)

            # ================================================================================== #
            # Display some information
            if should_trigger_by_steps(uidx, eidx, every_n_step=training_configs['disp_freq']):
                # words per second and sents per second
                words_per_sec = cum_words / (timer.toc(return_seconds=True))
                sents_per_sec = cum_samples / (timer.toc(return_seconds=True))
                lrate = list(optim.get_lrate())[0]

                summary_writer.add_scalar("Speed(words/sec)", scalar_value=words_per_sec, global_step=uidx)
                summary_writer.add_scalar("Speed(sents/sen)", scalar_value=sents_per_sec, global_step=uidx)
                summary_writer.add_scalar("lrate", scalar_value=lrate, global_step=uidx)
                summary_writer.add_scalar("oom_count", scalar_value=oom_count, global_step=uidx)

                # Reset timer
                timer.tic()
                cum_words = 0
                cum_samples = 0

            # ================================================================================== #
            # Saving checkpoints
            if should_trigger_by_steps(uidx, eidx, every_n_step=training_configs['save_freq'], debug=FLAGS.debug):
                model_collections.add_to_collection("uidx", uidx)
                model_collections.add_to_collection("eidx", eidx)
                model_collections.add_to_collection("bad_count", bad_count)

                if not is_early_stop:
                    checkpoint_saver.save(global_step=uidx, model=nmt_model, optim=optim, lr_scheduler=scheduler,
                                          collections=model_collections, ma=ma)

            # ================================================================================== #
            # Loss Validation & Learning rate annealing
            if should_trigger_by_steps(global_step=uidx, n_epoch=eidx, every_n_step=training_configs['loss_valid_freq'],
                                       debug=FLAGS.debug):
                if ma is not None:
                    origin_state_dict = deepcopy(nmt_model.state_dict())
                    nmt_model.load_state_dict(ma.export_ma_params(), strict=False)

                valid_loss = loss_validation(model=nmt_model,
                                             critic=critic,
                                             valid_iterator=valid_iterator,
                                             )

                model_collections.add_to_collection("history_losses", valid_loss)

                min_history_loss = np.array(model_collections.get_collection("history_losses")).min()

                summary_writer.add_scalar("loss", valid_loss, global_step=uidx)
                summary_writer.add_scalar("best_loss", min_history_loss, global_step=uidx)

                best_valid_loss = min_history_loss

                if ma is not None:
                    nmt_model.load_state_dict(origin_state_dict)
                    del origin_state_dict

                if optimizer_configs["schedule_method"] == "loss":
                    scheduler.step(metric=best_valid_loss)

            # ================================================================================== #
            # BLEU Validation & Early Stop

            if should_trigger_by_steps(global_step=uidx, n_epoch=eidx,
                                       every_n_step=training_configs['bleu_valid_freq'],
                                       min_step=training_configs['bleu_valid_warmup'],
                                       debug=FLAGS.debug):

                if ma is not None:
                    origin_state_dict = deepcopy(nmt_model.state_dict())
                    nmt_model.load_state_dict(ma.export_ma_params(), strict=False)

                valid_bleu = bleu_validation(uidx=uidx,
                                             valid_iterator=valid_iterator,
                                             batch_size=training_configs["bleu_valid_batch_size"],
                                             model=nmt_model,
                                             bleu_scorer=bleu_scorer,
                                             vocab_tgt=vocab_tgt,
                                             valid_dir=FLAGS.valid_path,
                                             max_steps=training_configs["bleu_valid_configs"]["max_steps"],
                                             beam_size=training_configs["bleu_valid_configs"]["beam_size"],
                                             alpha=training_configs["bleu_valid_configs"]["alpha"]
                                             )

                model_collections.add_to_collection(key="history_bleus", value=valid_bleu)

                best_valid_bleu = float(np.array(model_collections.get_collection("history_bleus")).max())

                summary_writer.add_scalar("bleu", valid_bleu, uidx)
                summary_writer.add_scalar("best_bleu", best_valid_bleu, uidx)

                # If model get new best valid bleu score
                if valid_bleu >= best_valid_bleu:
                    bad_count = 0

                    if is_early_stop is False:
                        # 1. save the best model
                        torch.save(nmt_model.state_dict(), best_model_prefix + ".final")

                        # 2. record all several best models
                        best_model_saver.save(global_step=uidx, model=nmt_model)
                else:
                    bad_count += 1

                    # At least one epoch should be traversed
                    if bad_count >= training_configs['early_stop_patience'] and eidx > 0:
                        is_early_stop = True
                        WARN("Early Stop!")

                summary_writer.add_scalar("bad_count", bad_count, uidx)

                if ma is not None:
                    nmt_model.load_state_dict(origin_state_dict)
                    del origin_state_dict

                INFO("{0} Loss: {1:.2f} BLEU: {2:.2f} lrate: {3:6f} patience: {4}".format(
                    uidx, valid_loss, valid_bleu, lrate, bad_count
                ))

        training_progress_bar.close()

        if ctx.ENABLE_CONTEXT:
            ctx.memory_cache = tuple()

        eidx += 1
        if eidx > training_configs["max_epochs"]:
            break


def translate(FLAGS):
    GlobalNames.USE_GPU = FLAGS.use_gpu

    config_path = os.path.abspath(FLAGS.config_path)

    with open(config_path.strip()) as f:
        configs = yaml.load(f)

    data_configs = configs['data_configs']
    model_configs = configs['model_configs']
    ctx.ENABLE_CONTEXT = model_configs['enable_history_context']
    ctx.GLOBAL_ENCODING = model_configs['enable_global_encoding']  
    ctx.GLOBAL_CAT = model_configs['global_encoder_cat']
    ctx.IS_INFERRING = True

    timer = Timer()
    # ================================================================================== #
    # Load Data

    INFO('Loading data...')
    timer.tic()

    # Generate target dictionary
    vocab_src = Vocabulary(**data_configs["vocabularies"][0])
    vocab_tgt = Vocabulary(**data_configs["vocabularies"][1])

    valid_dataset = TextLineDataset(data_path=FLAGS.source_path,
                                    vocabulary=vocab_src)

    valid_iterator = DataIterator(dataset=valid_dataset,
                                  batch_size=FLAGS.batch_size,
                                  use_bucket=True, buffer_size=100000, numbering=True)

    INFO('Done. Elapsed time {0}'.format(timer.toc()))

    # ================================================================================== #
    # Build Model & Sampler & Validation
    INFO('Building model...')
    timer.tic()
    model = build_model(n_src_vocab=vocab_src.max_n_words,
                            n_tgt_vocab=vocab_tgt.max_n_words, **model_configs)
    model.eval()
    INFO('Done. Elapsed time {0}'.format(timer.toc()))

    INFO('Reloading model parameters...')
    timer.tic()

    params = load_model_parameters(FLAGS.model_path, map_location="cpu")

    model.load_state_dict(params)

    if GlobalNames.USE_GPU:
        model.cuda()

    INFO('Done. Elapsed time {0}'.format(timer.toc()))

    INFO('Begin...')

    numbers = []
    result = []
    n_words = 0
    trans_docs = []

    timer.tic()

    infer_progress_bar = tqdm(total=len(valid_iterator),
                              desc=' - (Infer)  ',
                              unit="sents")

    valid_iter = valid_iterator.build_generator()
    for batch in valid_iter:

        seq_nums, seqs_x = batch
        numbers += seq_nums

        batch_size_t = len(seqs_x)

        if ctx.GLOBAL_ENCODING:
            x = prepare_data(seqs_x, cuda=GlobalNames.USE_GPU)
            # sents_mapping, max_n_sents = src_doc_sents_map(x)
            # enc_out, enc_mask = model.encoder(x)
            sents_mapping, sent_rank, _ = src_doc_sents_map(x)
            with torch.set_grad_enabled(False):
                enc_out, enc_mask = model.encoder(x, position=sent_rank, segment_ids=sents_mapping)

        if ctx.GLOBAL_CAT:  # extend mapping & mask to 2x because enc_output=[enc, glb_enc]
            sents_mapping = torch.cat([sents_mapping, sents_mapping], dim=1)
            enc_mask = torch.cat([enc_mask, enc_mask], dim=1)
        x_batch = prepare_data_doc(seqs_x)

        trans_sents2doc = []
        for i in range(len(seq_nums)):
            trans_sents2doc.append( [] )

        ctx.memory_cache = tuple()
        ctx.memory_mask = None

        for sents_no, x_sents in enumerate(x_batch):
            # mask all non-current sentences
            if ctx.GLOBAL_ENCODING:
                is_not_current_sents = sents_mapping.detach().ne(sents_no)
                current_sent_mask = torch.where(is_not_current_sents, is_not_current_sents, enc_mask)
            else:
                # encode current sentence
                with torch.set_grad_enabled(False):
                    enc_out, current_sent_mask = model.encoder(x_sents)

            with torch.no_grad():
                dec_state = {"ctx": enc_out, "ctx_mask": current_sent_mask}
                word_ids = beam_search(nmt_model=model, 
                                       dec_state=dec_state, 
                                       beam_size=FLAGS.beam_size, 
                                       max_steps=FLAGS.max_steps, 
                                       alpha=FLAGS.alpha)
            word_ids = word_ids.cpu().numpy().tolist()

            # Append result
            iter_num = 0
            for sent_t in word_ids:
                # only leave the best one
                x_tokens = [vocab_tgt.id2token(wid) for wid in sent_t[0] if wid != PAD and wid != EOS]
                if len(x_tokens) > 0:
                    trans_sents2doc[iter_num].append(vocab_tgt.tokenizer.detokenize(x_tokens))
                iter_num += 1
        #end of for-loop

        trans_docs.extend(trans_sents2doc)
        infer_progress_bar.update(batch_size_t)

    ctx.IS_INFERRING = False

    origin_order = np.argsort(numbers).tolist()
    trans_docs = [trans_docs[ii] for ii in origin_order]

    trans_sents = []
    for trans_doc in trans_docs:
        for trans_sent in trans_doc:
            trans_sents.append(trans_sent)
    #split doc trans results into sent

    infer_progress_bar.close()

    INFO('Done. Speed: {0:.2f} words/sec'.format(n_words / (timer.toc(return_seconds=True))))
    
    with open(FLAGS.saveto, "w") as fp:
        fp.write("\n".join(trans_sents))
