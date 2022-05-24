from torch.utils.data import DistributedSampler, DataLoader, Dataset
from librosa.util import normalize
from scipy.io import wavfile
from hparams import hparams as hps, symbols
from hgtk.text import compose
from typing import List, Union
import numpy as np
import torch
import librosa
import os
import re

_mel_basis = None
_symbol_to_id = {s: i for i, s in enumerate(symbols.symbols)}
_id_to_symbol = {i: s for i, s in enumerate(symbols.symbols)}

def convert_number(number: re.Match) -> str:
    pass

def decompose_hangul(hangle: re.Match) -> str:
    hangul = hangle.group(0)
    if re.match("[ㄱ-ㅎ]", hangul) is not None:
        if hangul in symbols.special_ja:
            hangul = symbols.special_ja[hangul]
        else:
            middle = "ㅣ ㅇㅡ"
            if hangul == "ㄱ":
                middle = "ㅣ ㅇㅕ"
            elif hangul == "ㄷ":
                middle = "ㅣ ㄱㅡ"
            elif hangul == "ㅅ":
                middle = "ㅣ ㅇㅗ"
            hangul = compose(hangul+middle+hangul+" ", compose_code=" ")
    elif re.match("[ㅏ-ㅣ]", hangul) is not None:
        hangul = compose("ㅇ"+hangul+" ", compose_code=" ")

    char_id = ord(hangul) - int('OxAC00', 16)
    cho_idx = char_id // 28 // 21
    joong_idx = char_id // 28 % 21
    jong_idx = char_id % 28
    return symbols.CHO[cho_idx] + symbols.JOONG[joong_idx] + symbols.JONG[jong_idx]


def prep_text(text: str, conv_alpha: bool = True) -> str:
    # lower -> change special words -> convert alphabet(optional) -> remove white space
    # -> remove non-eng/num/hangle/punc char -> convert number -> decompose hangle
    text = text.lower()
    for r_exp, word in symbols.convert_symbols:
        text = re.sub(r_exp, word, text)
    if conv_alpha:
        text = re.sub("[a-z]", lambda x: symbols.alpha_pron[x.group(0)], text)

    text = re.sub("\s+", " ", text)
    text = re.sub("^[0-9a-z가-힣ㄱ-ㅎㅏ-ㅣ ,.?!]", "", text)
    text = re.sub("[0-9]+", convert_number, text)
    text = re.sub("[가-힣ㄱ-ㅎㅏ-ㅣ]", decompose_hangul, text)
    return text

def text_to_sequence(text: str) -> List[int]:
    text = prep_text(text, hps.remove_alpha)
    return [_symbol_to_id[s] for s in text if s in _symbol_to_id]

def sequence_to_text(sequence) -> str:
    return "".join([_id_to_symbol[s] for s in sequence if s in _id_to_symbol])

def prepare_dataloaders(data_dir: str, n_gpu: int) -> torch.utils.data.DataLoader:
    trainset = ljdataset(data_dir)
    collate_fn = ljcollate(hps.n_frames_per_step)
    sampler = DistributedSampler(trainset) if n_gpu > 1 else None
    train_loader = DataLoader(trainset, num_workers=hps.n_workers, shuffle=n_gpu == 1,
                              batch_size=hps.batch_size, pin_memory=hps.pin_mem,
                              drop_last=True, collate_fn=collate_fn, sampler=sampler)
    return train_loader


# datasets
def _build_mel_basis():
    n_fft = (hps.num_freq - 1) * 2
    return librosa.filters.mel(hps.sample_rate, n_fft, n_mels=hps.num_mels, fmin=hps.fmin, fmax=hps.fmax)

def melspectrogram(y):
    # _stft(y)
    n_fft, hop_length, win_length = (hps.num_freq - 1) * 2, hps.frame_shift, hps.frame_length
    D = librosa.stft(y=y, n_fft=n_fft, hop_length=hop_length, win_length=win_length, pad_mode='reflect')

    # _amp_to_db(_linear_to_mel(np.abs(D)))
    global _mel_basis
    if _mel_basis is None:
        _mel_basis = _build_mel_basis()
    return np.log(np.maximum(1e-5, np.dot(_mel_basis, np.abs(D))))

def inv_melspectrogram(mel):
    # mel = _db_to_amp(mel)
    mel = np.exp(mel)

    # S = _mel_to_linear(mel)
    global _mel_basis
    if _mel_basis is None:
        _mel_basis = _build_mel_basis()
    inv_mel_basis = np.linalg.pinv(_mel_basis)
    inverse = np.dot(inv_mel_basis, mel)
    S = np.maximum(1e-10, inverse)

    # _griffin_lim(S ** hps.power)
    n_fft, hop_length, win_length = (hps.num_freq - 1) * 2, hps.frame_shift, hps.frame_length
    angles = np.exp(2j * np.pi * np.random.rand(*S.shape))
    S_complex = np.abs(S).astype(np.complex)
    y = librosa.istft(S_complex * angles, hop_length=hop_length, win_length=win_length)
    for i in range(hps.gl_iters):
        stft = librosa.stft(y=y, n_fft=n_fft, hop_length=hop_length, win_length=win_length, pad_mode='reflect')
        angles = np.exp(1j * np.angle(stft))
        y = librosa.istft(S_complex * angles, hop_length=hop_length, win_length=win_length)
    return np.clip(y, a_max=1, a_min=-1)


def get_text(text):
    return torch.IntTensor(text_to_sequence(text))

def get_mel(wav_path):
    sr, wav = wavfile.read(wav_path)
    assert sr == hps.sample_rate
    wav = normalize(wav/hps.MAX_WAV_VALUE)*0.95
    return torch.Tensor(melspectrogram(wav).astype(np.float32))

def get_mel_text_pair(text, wav_path):
    text = get_text(text)
    mel = get_mel(wav_path)
    return text, mel

def files_to_list(fdir) -> List[List[str, Union[str, torch.Tensor]]]:
    f_list = []
    for data_dir in os.listdir(fdir):
        with open(os.path.join(fdir, data_dir, 'transcript.txt'), encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split('|')
                wav_path = os.path.join(fdir, data_dir, '%s.wav' % parts[0])
                if hps.prep:
                    f_list.append(get_mel_text_pair(parts[1], wav_path))
                else:
                    f_list.append([parts[1], wav_path])
    return f_list


class ljdataset(Dataset):
    def __init__(self, fdir):
        self.f_list = files_to_list(fdir)

    def __getitem__(self, index):
        text, mel = self.f_list[index] if hps.prep else get_mel_text_pair(*self.f_list[index])
        return text, mel

    def __len__(self):
        return len(self.f_list)

class ljcollate:
    def __init__(self, n_frames_per_step):
        self.n_frames_per_step = n_frames_per_step

    def __call__(self, batch):
        # Right zero-pad all one-hot text sequences to max input length
        input_lengths, ids_sorted_decreasing = torch.sort(torch.LongTensor([len(x[0]) for x in batch]), dim=0, descending=True)
        max_input_len = input_lengths[0]

        text_padded = torch.LongTensor(len(batch), max_input_len)
        text_padded.zero_()
        for i in range(len(ids_sorted_decreasing)):
            text = batch[ids_sorted_decreasing[i]][0]
            text_padded[i, :text.size(0)] = text

        # Right zero-pad mel-spec
        num_mels = batch[0][1].size(0)
        max_target_len = max([x[1].size(1) for x in batch])
        if max_target_len % self.n_frames_per_step != 0:
            max_target_len += self.n_frames_per_step - max_target_len % self.n_frames_per_step
            assert max_target_len % self.n_frames_per_step == 0

        # include mel padded and gate padded
        mel_padded = torch.FloatTensor(len(batch), num_mels, max_target_len)
        mel_padded.zero_()
        gate_padded = torch.FloatTensor(len(batch), max_target_len)
        gate_padded.zero_()
        output_lengths = torch.LongTensor(len(batch))
        for i in range(len(ids_sorted_decreasing)):
            mel = batch[ids_sorted_decreasing[i]][1]
            mel_padded[i, :, :mel.size(1)] = mel
            gate_padded[i, mel.size(1)-1:] = 1
            output_lengths[i] = mel.size(1)

        return text_padded, input_lengths, mel_padded, gate_padded, output_lengths
