import music21
import torch

from fastai.distributed import *
from fastai.text.models.transformer import *

import numpy as np

from .fastai_data import *
from .lmnp_transformer import *
from .encode_data import *

import uuid

# source_dir = 'midi_encode/np/shortdur'
# path = Path('../../data/midi/v9/')/source_dir
# out_path = Path('../../data/generated/')
    

def v13_config(vocab_path):
    VOCAB_SZ = create_vocab_sizes(vocab_path)
    
    config = tfmerXL_lm_config.copy()
    
    config['pad_idx'] = PADDING_IDX+ENC_OFFSET
    config['bos_idx'] = VALTBOS+ENC_OFFSET
    config['sep_idx'] = VALTSEP+ENC_OFFSET
    config['enc_offset'] = ENC_OFFSET
    config['transpose_range'] = (0,12)
    config['act'] = Activation.GeLU
    # config['act'] = Activation.ReLU

    config['mem_len'] = 512

    config['bs'] = 16
    config['bptt'] = 256
    
    emb_size = 768
    config['d_model'] = emb_size
    config['vocab_size'] = sum(VOCAB_SZ)
    config['single_stream'] = True
    config['d_inner'] = 2048
    config['n_layers'] = 16
    
    config['n_heads'] = 12
    config['d_head'] = 64

#     config['embed_p'] = 0.3
#     config['attn_p'] = 0.15 # attention dropout
#     config['output_p'] = 0.15 # decoder dropout (before final linear layer)


    return config

def v13s_config(vocab_path):
    config = v13_config(vocab_path)
    emb_size = 256
    config['n_heads'] = 8
    config['d_head'] = 32
    config['d_model'] = emb_size
    config['d_inner'] = 2048
    return config
    

def load_music_data(path, cache_name, enc_offset, pad_idx, bos_idx, transpose_range, single_stream=False, **kwargs):
    transpose_tfm = partial(rand_transpose, enc_offset=enc_offset, rand_range=transpose_range)
    category_tfm = partial(rand_category, pad_idx=pad_idx, bos_idx=bos_idx)
    if single_stream:
        data = LMNPDataBunch.load(path=path, cache_name=cache_name, **kwargs, 
                                  train_tfms=[transpose_tfm, category_tfm, to_single_stream], valid_tfms=[to_single_stream])
    else:
        data = LMNPDataBunch.load(path=path, cache_name=cache_name, **kwargs, train_tfms=[transpose_tfm, category_tfm])
    return data

def load_learner(data, config, load_path=None):
    learn = language_model_learner(data, config, clip=0.25)
    if load_path:
        state = torch.load(load_path, map_location='cpu')
        get_model(learn.model).load_state_dict(state['model'], strict=False)
    return learn


# Serving functions


# NOTE: looks like npenc does not include the separator. 
# This means we don't have to remove the last (separator) step from the seed in order to keep predictions
def predict_from_midi(learn, midi=None, n_words=600, 
                      temperatures=(1.5,0.9), min_ps=(1/128,0.0), category=None, **kwargs):
    seed_np = midi2npenc(midi, category=category) # music21 can handle bytes directly
    xb = torch.tensor(to_single_stream(seed_np))[None]
    pred, seed = learn.predict(xb, n_words=n_words, temperatures=temperatures, min_ps=min_ps)
    seed = to_double_stream(seed)
    pred = to_double_stream(pred)
    full = np.concatenate((seed,pred), axis=0)
    
    return pred, seed, full



# Deprecated song list - moved to s3

import pandas as pd
def get_htlist(path, source_dir, use_cache=True):
    json_path = path/'htlist.json'
    if use_cache and json_path.exists():
        with open(json_path, 'r') as fp:
            htlist = json.load(fp)
    else:
        df = pd.read_csv(path/'midi_encode.csv')
        df = df.loc[df[source_dir].notna()] # make sure it exists
        df = df.loc[df.source == 'hooktheory'] # hooktheory only
        df = df.rename(index=str, columns={source_dir: 'numpy'}) # shortdur -> numpy
        df = df.reindex(index=df.index[::-1]) # A's first
        df = df.where((pd.notnull(df)), None) # nan values break json

        htlist = df.to_dict('records') # row format
        htlist = [format_htsong(s) for s in htlist] # normalize artist, title, create song ID
        htlist = { s['sid']:s for s in htlist}
        with open(json_path, 'w') as fp:
            json.dump(htlist, fp)
    return htlist

def format_htsong(s):
    s = s.copy()
    s['title'] = s['title'].title().replace('-', ' ')
    s['artist'] = s['artist'].title().replace('-', ' ')
    s['sid'] = str(hash(s['midi']))
    return s

def search_htlist(htlist, keywords='country road', max_results=10):
    keywords = keywords.split(' ')
    def contains_keywords(f): return all([k in str(f) for k in keywords])
    res = []
    for k,s in htlist.items():
        if contains_keywords(s['numpy']): res.append(s)
        if len(res) >= max_results: break
    return res

def get_filelist(path):
    files = get_files(path/'hooktheory', extensions=['.npy'], recurse=True)
    return files





# New way 

import hashlib
import shutil

def df2records(path):
    df = pd.read_csv(path/'midi_encode.csv')
    df = df.loc[df[source_dir].notna()] # make sure it exists
    df = df.loc[df.source == 'hooktheory'] # hooktheory only
    df = df.rename(index=str, columns={source_dir: 'numpy'}) # shortdur -> numpy
    df = df.reindex(index=df.index[::-1]) # A's first
    df = df.where((pd.notnull(df)), None) # nan values break json
    return df.to_dict('records')

def format_meta(s):
    title = s['title'].title().replace('-', ' ')
    artist = s['artist'].title().replace('-', ' ')
    display = ' - '.join([title, artist])
    if s.get('section'): display += ' - ' + s['section'].title()
    sid = hashlib.md5(display.encode('utf-8')).hexdigest()
    
    source_file = file_path/data_dir/s['midi']
    to_file = encoded_path/f'{sid[::-1]}.mid'
    if not to_file.exists():
        shutil.copy(str(source_file), str(to_file))
    
    return {
        'title': title,
        'artist': artist,
        'bpm': s['ht_bpm'],
        'display': display,
        'genres': s['genres'],
        'sid': sid
    }

def build_db(path):
    recordlist = df2records(path)
    htlist = [format_meta(s) for s in recordlist]
    json_path = file_path/'data/assets/json/htlist.json'
    with open(json_path, 'w') as fp:
        json.dump(htlist, fp, separators=(',', ':'))
    return htlist
