"""Official FP32 inference with a declared 200-second prefix cap."""

import hashlib
import io
import math
import sys
import threading
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from scipy.signal import resample_poly

from ...schema import digest

DEFINITION = dict(
    task="spoken_language",
    schema_version="firered-lid-v1",
    precision="float32_tf32_disabled",
    decode="libsndfile_float32",
    channels="arithmetic_mean_float32",
    resample="scipy_resample_poly_default_kaiser_5_constant",
    model_rate=16000,
    amplitude="float_waveform_times_32768_no_clip",
    windows="single_native_interval_first_min_duration_200s_no_splitting",
    max_analysis_seconds=200,
    waveform_padding="none",
    feature_padding="official_length_masked_zero_padding",
    beam_size=3,
    nbest=1,
    decode_max_len=2,
    softmax_smoothing=1.25,
    aed_length_penalty=0.6,
    eos_penalty=1.0,
    confidence="official_mean_selected_token_probabilities_unrounded_not_calibrated",
    language_mapping="first_token; zh_yue_to_yue; other_to_und; iw_to_he; jw_to_jv; tl_to_fil",
)


class InvalidAudio(ValueError):
    """Per-sample audio defects; model/configuration failures are not caught here."""


def language(raw):
    parts = raw.split()
    if not parts or any(p.startswith("<") for p in parts):
        raise ValueError(f"Invalid model language label: {raw!r}")
    if parts[0] == "zh" and "yue" in parts[1:]:
        return "yue"
    return {"other": "und", "iw": "he", "jw": "jv", "tl": "fil"}.get(parts[0], parts[0])


def intervals(frames, rate):
    return [(0, min(frames, 200 * rate))]


def official_imports(code):
    # Import the upstream standalone LID package, avoiding the ASR/VAD system facade.
    sys.path.insert(0, str(Path(code) / "fireredasr2s"))
    from fireredlid.data.feat import FeatExtractor
    from fireredlid.models.fireredlid_aed import FireRedLidAed
    from fireredlid.tokenizer.lid_tokenizer import LidTokenizer

    return FeatExtractor, FireRedLidAed, LidTokenizer


class Frontend:
    def __init__(self, model_dir, code):
        self.extractor_type, _, _ = official_imports(code)
        self.model_dir = Path(model_dir)
        self.local = threading.local()

    def features(self, wave, rate):
        if rate != 16000:
            divisor = math.gcd(rate, 16000)
            wave = resample_poly(wave, 16000 // divisor, rate // divisor).astype(np.float32)
        if len(wave) < 400:
            raise InvalidAudio("audio_too_short_for_fbank")
        if not hasattr(self.local, "extractor"):
            self.local.extractor = self.extractor_type(str(self.model_dir / "cmvn.ark"))
        extractor = self.local.extractor
        features, lengths, _, _, _ = extractor([[16000, wave * 32768.0]], ["window"])
        if features is None or not torch.isfinite(features).all():
            raise InvalidAudio("audio_invalid_fbank")
        return features[0, : lengths[0]], len(wave)

    def prepare(self, row, profile_id):
        data = row["audio"]["bytes"]
        if hashlib.sha256(data).hexdigest() != row["audio_sha256"]:
            raise RuntimeError("Source audio hash conflict")
        record = dict(
            sample_id=row["sample_id"],
            input_fingerprint=digest(
                dict(
                    sample_id=row["sample_id"],
                    audio_sha256=row["audio_sha256"],
                    analysis="first_min_duration_200s",
                    source_language=row["language"],
                    profile_id=profile_id,
                )
            ),
            status="ok",
            error_code=None,
            result=None,
        )
        try:
            try:
                wave, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
            except (sf.LibsndfileError, RuntimeError, ValueError) as exc:
                raise InvalidAudio("audio_decode_failed") from exc
            if not len(wave):
                raise InvalidAudio("audio_empty")
            if not np.isfinite(wave).all():
                raise InvalidAudio("audio_nonfinite")
            frames, channels = wave.shape
            wave = wave.mean(axis=1, dtype=np.float32)
            start, end = intervals(frames, rate)[0]
            feature, resampled_frames = self.features(wave[start:end], rate)
            record["result"] = dict(
                audio_sha256=row["audio_sha256"],
                source_language=row["language"],
                native_sample_rate=rate,
                channels=channels,
                decoded_num_samples=frames,
                declared_num_samples=row["num_frames"],
                start_sample=0,
                end_sample=end,
                analysis_truncated=end < frames,
                model_sample_rate=16000,
                model_num_samples=resampled_frames,
            )
            return record, [feature]
        except InvalidAudio as exc:
            record.update(status="failed", error_code=str(exc))
            return record, []


class Encoder:
    def __init__(self, model_dir, code, device="cuda:0"):
        _, model_type, tokenizer_type = official_imports(code)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.set_num_threads(1)
        package = torch.load(
            Path(model_dir) / "model.pth.tar", map_location="cpu", weights_only=False
        )
        self.model = model_type.from_args(package["args"])
        self.model.load_state_dict(package["model_state_dict"], strict=True)
        self.model.eval().to(device)
        self.device = device
        self.tokenizer = tokenizer_type(str(Path(model_dir) / "dict.txt"))

    @torch.inference_mode()
    def predict(self, features):
        lengths = torch.tensor([len(f) for f in features], device=self.device)
        padded = torch.nn.utils.rnn.pad_sequence(features, batch_first=True).to(self.device)
        hypotheses = self.model.process(padded, lengths, 3, 1, 2, 1.25, 0.6, 1.0)
        if len(hypotheses) != len(features):
            raise RuntimeError("Incomplete model batch")
        output = []
        for candidates in hypotheses:
            hyp = candidates[0]
            raw = self.tokenizer.detokenize([int(v) for v in hyp["yseq"].cpu()])
            confidence = float(hyp["confidence"].cpu())
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                output.append(dict(error_code="model_invalid_confidence"))
                continue
            try:
                normalized = language(raw)
            except ValueError:
                output.append(dict(error_code="model_invalid_language"))
                continue
            output.append(dict(raw_language=raw, language=normalized, confidence=confidence))
        if len(output) >= 4 and all("error_code" in item for item in output):
            raise RuntimeError("Entire model batch invalid; investigate before continuing")
        return output

    def predict_bounded(self, features):
        try:
            return self.predict(features)
        except torch.cuda.OutOfMemoryError:
            if len(features) == 1:
                raise
        torch.cuda.empty_cache()
        middle = len(features) // 2
        return self.predict_bounded(features[:middle]) + self.predict_bounded(features[middle:])


def infer_records(encoder, prepared, batch_size=32, frame_budget=32000):
    records = [record for record, _ in prepared]
    items = sorted(
        [
            (len(f), i, j, f)
            for i, (_, features) in enumerate(prepared)
            for j, f in enumerate(features)
        ],
        key=lambda x: x[0],
    )
    offset = 0
    while offset < len(items):
        end = offset + 1
        while end < len(items) and end - offset < batch_size:
            if items[end][0] * (end - offset + 1) > frame_budget:
                break
            end += 1
        batch = items[offset:end]
        predictions = encoder.predict_bounded([item[3] for item in batch])
        for (_, i, j, _), prediction in zip(batch, predictions, strict=True):
            if "error_code" in prediction:
                records[i].update(status="failed", error_code=prediction["error_code"], result=None)
            else:
                records[i]["result"].update(prediction)
        offset = end
    for record in records:
        result = record["result"]
        if result is not None:
            lang = result["language"]
            source = (result["source_language"] or "").lower().split("-")[0]
            result["source_language_mismatch"] = (
                lang != source if source and lang != "und" else None
            )
    return records
