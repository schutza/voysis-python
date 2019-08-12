import json
import os

import numpy as np
import tarfile
from scipy.special import logsumexp

from voysis.audio.keyword_detector import KeywordDetector


MAX_LOW_AMPLITUDE_FLOAT = 0.001
MIN_LOW_AMPLITUDE_FLOAT = -MAX_LOW_AMPLITUDE_FLOAT

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'


class WakewordDetector:
    def __init__(self, model_archive_path: str, print_x_delay: int = 5):
        model_path = self._unzip_model(model_archive_path)
        assets_filename = os.path.join(model_path, "assets", "wakeword_assets.json")
        with open(assets_filename, 'r') as assets_fp:
            config = json.load(assets_fp)
        self._activation_threshold = config["activation_threshold"]
        self._sensitivity = config["sensitivity"]
        self._trigger_level = config["trigger_level"]
        self._num_samples = config["window_size"]
        self._sample_stride = config["sample_stride"]
        self._drop_first_mfcc = config["drop_first_mfcc"]
        self._preemphasis = config["preemphasis"]
        self._normalise = config["normalise"]
        self._batch_norm = config["batch_norm"]
        self._input_node_name = config["input_node_name"]
        self._output_prediction_node_name = config["output_prediction_node_name"]
        self._input_logits_node_name = config["input_logits_node_name"]
        self._keyword_detector = KeywordDetector(
            model_path,
            self._input_node_name,
            self._output_prediction_node_name,
            self._input_logits_node_name,
            self._drop_first_mfcc,
            self._preemphasis,
            self._normalise,
            self._batch_norm)
        if self._sample_stride <= 0:
            raise ValueError("Sample stride must be greater than zero.")
        self._rescaling_max = 0.999
        self._sample_stride_bytes = self._sample_stride * 2
        self._num_bytes = self._num_samples * 2
        self._buffered_audio = bytes()
        self._trigger_count = 0
        # It seems that printing once every 4000 samples is a nice pace.
        self._print_level = 4000 // self._sample_stride
        self._print_x_delay = print_x_delay
        # Won't attempt to restrict the output if the sample stride is above 4000.
        if self._print_level == 0:
            self._print_level = 1
            self._print_x_delay = 0


    def _unzip_model(self, archive_path: str):
        dir_name = os.path.dirname(archive_path)
        model_dir = os.path.join(dir_name, os.path.basename(archive_path).split(".")[0])
        if not os.path.isdir(model_dir):
            with tarfile.open(archive_path) as tfp:
                tfp.extractall()
        return model_dir

    def stream_audio(self, frame_generator):
        self._buffered_audio = bytes()
        for frame in frame_generator:
            audio = self._buffered_audio + frame
            while len(audio) >= self._num_bytes:
                audio_to_process = audio[:self._num_bytes]
                audio = audio[self._sample_stride_bytes:]
                samples = np.fromstring(audio_to_process, dtype='<i2').astype(np.float32, order='C') / 32768.0
                logits, _ = self._keyword_detector.decode(samples)
                sm_output = self._softmax(logits, axis=-1)
                act_list = self._calc_activations(sm_output)
                triggered = self._check_trigger(act_list)
                if triggered:
                    return True
            self._buffered_audio = audio
        return False


    def test_wakeword(self, frame_generator):
        first_loop = True
        prints_since_x = self._print_x_delay + 1
        counter = 0
        wakeword_indices = []
        predictions = []
        triggers = []
        self._buffered_audio = bytes()
        for frame in frame_generator:
            audio = self._buffered_audio + frame
            while len(audio) >= self._num_bytes:
                audio_to_process = audio[:self._num_bytes]
                audio = audio[self._sample_stride_bytes:]
                samples = np.fromstring(audio_to_process, dtype='<i2').astype(np.float32, order='C') / 32768.0
                if first_loop:
                    self._check_samples(samples)
                logits, _ = self._keyword_detector.decode(samples)
                sm_output = self._softmax(logits, axis=-1)
                act_list = self._calc_activations(sm_output)
                triggered = self._check_trigger(act_list)
                triggers.append(int(triggered))
                if triggered:
                    wakeword_indices.append(counter)
                    predictions.append(act_list)
                counter += 1
                if counter % self._print_level == 0:
                    if sum(triggers) > 0 and prints_since_x > self._print_x_delay:
                        print('X', end='', flush=True)
                        prints_since_x = 0
                    else:
                        print('_', end='', flush=True)
                    prints_since_x += 1
                    triggers = []
                first_loop = False
            self._buffered_audio = audio
        # Ensure there is a newline after wakeword output finishes.
        print()
        return wakeword_indices, predictions

    def _check_samples(self, samples):
        for sample in samples:
            if sample > MAX_LOW_AMPLITUDE_FLOAT or sample < MIN_LOW_AMPLITUDE_FLOAT:
                return
        raise RuntimeError("The input audio has low input volume. Please check your microphone settings and try again.")

    def _check_trigger(self, act_list):
        if sum(act_list) >= self._activation_threshold:
            self._trigger_count += 1
        else:
            self._trigger_count = 0
        if self._trigger_count >= self._trigger_level:
            self._trigger_count = 0
            return True
        else:
            return False

    def _calc_activations(self, sm_output):
        actlist = []
        for value in sm_output:
            pos_value = value[1]
            if pos_value > (1 - self._sensitivity):
                actlist.append(1)
            else:
                actlist.append(0)
        return actlist

    def _softmax(self, x: np.ndarray, axis=None):
        """
        Applies softmax to the input matrix along the specified axis.

        Args:
            x: Input array.

        Returns:
            x with the softmax operation applied.
        """
        return np.exp(x - logsumexp(x, axis=axis, keepdims=True))
