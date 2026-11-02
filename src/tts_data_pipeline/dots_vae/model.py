"""dots.tts AudioVAE posterior + CAM++ x-vector, reproducing the dots training inputs.

The waveform chain calls the pinned upstream functions themselves (BasicTtsPipeline and
DotsTtsModel.prepare_training_inputs at the profile's revision), not a reimplementation:
decode -> channel mean -> kaiser resample to 48 kHz -> edge silence to 250 ms -> zero pad to
a multiple of 7680. Latents come from that padded waveform plus a zero tail, so the last two
frames see a computed lookahead as in a dots batch row that is not the longest. The x-vector
uses the training fbank (kaiser 48k->16k), cropped to the first 10 s in place of dots' random
crop, then CAM++.
"""

import io
import os
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

DOTS_SOURCE = Path(
    os.environ.get(
        "TTS_DOTS_SOURCE", Path(__file__).resolve().parents[3] / "cache/third_party/dots.tts"
    )
)
DOTS_REVISION = "4cf7d6b13f42066f97784e7d7cf2a9ed5f956997"
# Upstream files whose code decides the stored values.
DOTS_FILES = (
    "src/dots_tts/data/pipelines/preprocessing.py",
    "src/dots_tts/data/pipelines/tts_pipeline.py",
    "src/dots_tts/models/dots_tts/model.py",
    "src/dots_tts/modules/speaker/campplus.py",
    "src/dots_tts/modules/speaker/campplus_layers.py",
    "src/dots_tts/modules/speaker/encoder.py",
    "src/dots_tts/modules/speaker/fbank.py",
    "src/dots_tts/modules/vocoder/bigvgan.py",
    "src/dots_tts/modules/vocoder/alias_free_act.py",
    "src/dots_tts/modules/vocoder/alias_free_filter.py",
    "src/dots_tts/modules/vocoder/alias_free_resample.py",
    "src/dots_tts/modules/vocoder/config.py",
    "src/dots_tts/utils/audio.py",
)
MODEL_FILES = (
    "config.json",
    "vocoder.safetensors",
    "speaker_encoder.safetensors",
    "latent_stats.pt",
)
SAMPLE_RATE = 48000
HOP = 1920
ALIGN = 7680
LOOKAHEAD_TAIL = 2 * HOP
XVEC_SECONDS = 10.0
# One encoder call holds at most this many padded 48 kHz samples (about 4 minutes).
BATCH_SAMPLES = 48000 * 240
MAX_BATCH = 32

if str(DOTS_SOURCE / "src") not in sys.path:
    sys.path.insert(0, str(DOTS_SOURCE / "src"))


def upstream():
    from dots_tts.data.pipelines.preprocessing import (
        DEFAULT_EDGE_SILENCE_MS,
        DEFAULT_EDGE_SILENCE_TOP_DB,
        normalize_edge_silence_duration,
        pad_waveform_align_only,
    )
    from dots_tts.models.dots_tts.config import ModelConfig
    from dots_tts.modules.speaker.encoder import SpeakerXVectorFeatures
    from dots_tts.modules.speaker.fbank import extract_speaker_fbank
    from dots_tts.modules.vocoder.bigvgan import AudioVAE
    from dots_tts.utils.audio import high_quality_resample

    return dict(
        silence_ms=DEFAULT_EDGE_SILENCE_MS,
        silence_top_db=DEFAULT_EDGE_SILENCE_TOP_DB,
        normalize_edge=normalize_edge_silence_duration,
        pad=pad_waveform_align_only,
        ModelConfig=ModelConfig,
        Speaker=SpeakerXVectorFeatures,
        fbank=extract_speaker_fbank,
        AudioVAE=AudioVAE,
        resample=high_quality_resample,
    )


def edge_deltas(waveform, up):
    """Signed samples the upstream edge step adds (+) or trims (-) at each end."""
    target = int(round(SAMPLE_RATE * up["silence_ms"] / 1000.0))
    amplitude = waveform[0].abs()
    threshold = float(amplitude.max()) * (10.0 ** (-float(up["silence_top_db"]) / 20.0))
    loud = torch.nonzero(amplitude > threshold, as_tuple=False).flatten()
    first, last = int(loud[0]), int(loud[-1])
    return target - first, target - (waveform.size(-1) - last - 1)


def load_strict(module, path):
    from safetensors.torch import load_file

    result = module.load_state_dict(load_file(path, device="cpu"), strict=False)
    if result.missing_keys or result.unexpected_keys:
        raise ValueError(f"Weights do not match {path.name}: {result}")
    return module


class Encoder:
    def __init__(self, model_root, device):
        import json

        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        self.up = upstream()
        root = Path(model_root)
        config = self.up["ModelConfig"](**json.loads((root / "config.json").read_text()))
        self.device = torch.device(device)
        vae = self.up["AudioVAE"](config.vocoder).eval()
        # Released weights are stored with weight norm folded.
        vae.remove_weight_norm()
        self.vae = load_strict(vae, root / "vocoder.safetensors").to(self.device)
        speaker = self.up["Speaker"](
            sample_rate=SAMPLE_RATE,
            campplus_embedding_size=config.campplus_embedding_size,
            max_audio_seconds=config.xvec_max_audio_seconds,
        ).eval()
        self.speaker = load_strict(speaker, root / "speaker_encoder.safetensors").to(self.device)
        if self.vae.hop_size != HOP or self.vae.sample_rate != SAMPLE_RATE:
            raise ValueError("Unexpected AudioVAE geometry")
        if config.xvec_max_audio_seconds != XVEC_SECONDS:
            raise ValueError("Unexpected x-vector window")
        self.hardware = dict(
            device=torch.cuda.get_device_name(self.device) if self.device.type == "cuda" else "cpu",
            torch=torch.__version__,
            cuda=torch.version.cuda,
            cudnn=torch.backends.cudnn.version(),
        )

    def waveform(self, row):
        """Training waveform with its alignment to the decoded native audio."""
        import hashlib

        raw = row["audio"]["bytes"]
        if hashlib.sha256(raw).hexdigest() != row["audio_sha256"]:
            raise ValueError("Audio bytes differ from audio_sha256")
        data, rate = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
        if rate != row["sample_rate"] or data.shape[1] != row["channels"]:
            raise ValueError("Decoded audio differs from the base header")
        if not np.isfinite(data).all():
            return None, dict(end_frame=len(data)), "nonfinite_audio"
        waveform = torch.from_numpy(data.T)
        if waveform.size(0) > 1:
            waveform = waveform.mean(dim=0, keepdim=True)
        waveform = self.up["resample"](waveform.contiguous(), orig_sr=rate, target_sr=SAMPLE_RATE)
        info = dict(end_frame=len(data), resampled_frames=int(waveform.size(-1)))
        if waveform.numel() == 0 or float(waveform.abs().max()) <= 0.0:
            return None, info, "silent_audio"
        leading, trailing = edge_deltas(waveform, self.up)
        edged = self.up["normalize_edge"](waveform, sample_rate=SAMPLE_RATE)
        expected = waveform.size(-1) + leading + trailing
        if edged.size(-1) != expected:
            raise ValueError("Edge offsets do not reproduce the upstream length")
        padded = self.up["pad"](edged, multiple_of=ALIGN)
        info.update(
            edge_leading_delta=leading,
            edge_trailing_delta=trailing,
            processed_frames=int(edged.size(-1)),
            padded_frames=int(padded.size(-1)),
        )
        return padded, info, None

    @torch.no_grad()
    def latents(self, waveforms):
        """Posterior [T,256] per waveform; each row is followed by at least a zero tail."""
        width = max(w.size(-1) for w in waveforms) + LOOKAHEAD_TAIL
        batch = torch.zeros(len(waveforms), 1, width)
        for i, w in enumerate(waveforms):
            batch[i, 0, : w.size(-1)] = w[0]
        out = self.vae.extract_latents(batch.to(self.device)).float().cpu()
        return [out[i, :, : w.size(-1) // HOP].T.contiguous() for i, w in enumerate(waveforms)]

    def fbank(self, waveform):
        return self.up["fbank"](waveform, sample_rate=SAMPLE_RATE)

    @torch.no_grad()
    def campplus(self, waveform, fbank):
        """One sample at a time: CAM++ does not mask zero-padded fbank batches."""
        length = torch.tensor([waveform.size(-1)])
        cropped, frames = self.speaker._crop_fbank(
            fbank[None],
            torch.tensor([fbank.size(0)]),
            length,
            torch.clamp(length, max=round(SAMPLE_RATE * XVEC_SECONDS)),
            torch.zeros_like(length),
        )
        out = self.speaker.model(cropped.to(self.device), lengths=frames.to(self.device))
        return out[0].float().cpu(), int(frames[0])

    def extract(self, rows, pool=None):
        """[(info, posterior[T,256] | None, xvector[512] | None, error | None)] in row order.

        `pool` runs the CPU waveform chain (decode, kaiser resample, fbank) in threads.
        """
        mapper = pool.map if pool is not None else map
        prepared = list(mapper(self.waveform, rows))
        good = [i for i, (_, _, e) in enumerate(prepared) if e is None]
        results = [(info, None, None, error) for _, info, error in prepared]
        fbanks = dict(zip(good, mapper(lambda i: self.fbank(prepared[i][0]), good), strict=True))
        for chunk in batches([prepared[i][0].size(-1) for i in good]):
            idx = [good[j] for j in chunk]
            posteriors = self.latents([prepared[i][0] for i in idx])
            for i, post in zip(idx, posteriors, strict=True):
                vector, frames = self.campplus(prepared[i][0], fbanks[i])
                info = dict(prepared[i][1], xvector_fbank_frames=frames)
                results[i] = (info, post.numpy(), vector.numpy(), None)
        return results


def batches(lengths):
    """Length-sorted groups under the sample budget; positions refer to `lengths`."""
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    groups, current = [], []
    for i in order:
        width = lengths[i] + LOOKAHEAD_TAIL
        if current and (len(current) >= MAX_BATCH or width * (len(current) + 1) > BATCH_SAMPLES):
            groups.append(current)
            current = []
        current.append(i)
    if current:
        groups.append(current)
    return groups
