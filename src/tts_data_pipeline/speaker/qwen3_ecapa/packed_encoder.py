"""No-padding extraction over concatenated real frames, with bounded CPU prefetch."""

import time
from collections import Counter, deque

import numpy as np
import torch

from .audio import resampled_frames
from .encoder import Encoder as NativeEncoder
from .frontend import decode_waveform
from .mel import GPUMel
from .packed import Packed

MAX_BATCH = 64
MEL_FRAME_BUDGET = 90000


class Encoder(NativeEncoder):
    def __init__(self, model_root, gpu, memory_fraction=0.25):
        super().__init__(model_root, gpu, memory_fraction)
        self.frontend = GPUMel(self.device)
        self.network = Packed(self.model)
        self.encode_seconds = 0.0
        self.batch_sizes = Counter()
        self.packed([torch.zeros(1280, device=self.device), torch.zeros(24000, device=self.device)])

    def packed(self, waves):
        return self.network(self.frontend(waves))

    def extract(self, rows, decoders):
        indices = sorted(
            range(len(rows)),
            key=lambda i: resampled_frames(rows[i]["num_frames"], rows[i]["sample_rate"]),
        )
        batches = []
        batch = []
        frames = 0
        for i in indices:
            length = resampled_frames(rows[i]["num_frames"], rows[i]["sample_rate"]) // 256
            if batch and (len(batch) == MAX_BATCH or frames + length > MEL_FRAME_BUDGET):
                batches.append(batch)
                batch = []
                frames = 0
            batch.append(i)
            frames += length
        if batch:
            batches.append(batch)
        iterator = iter(batches)
        queue = deque()
        results = [None] * len(rows)

        def submit():
            ids = next(iterator, None)
            if ids is not None:
                queue.append((ids, [decoders.submit(decode_waveform, rows[i]) for i in ids]))

        submit()
        submit()
        while queue:
            ids, futures = queue.popleft()
            decoded = [f.result() for f in futures]
            submit()
            valid = [
                (i, mel, info)
                for i, (mel, info, error) in zip(ids, decoded, strict=True)
                if error is None
            ]
            start = time.perf_counter()
            if valid:
                output = self.packed([mel.to(self.device) for i, mel, info in valid]).cpu().numpy()
                assert output.shape == (len(valid), 1024) and np.isfinite(output).all()
                for (i, mel, info), y in zip(valid, output, strict=True):
                    results[i] = (info, y, None)
                self.batch_sizes[len(valid)] += 1
            self.encode_seconds += time.perf_counter() - start
            for i, (_, info, error) in zip(ids, decoded, strict=True):
                if error:
                    results[i] = (info, None, error)
        return results
