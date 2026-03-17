/// Energy-based Voice Activity Detector.
///
/// Algorithm:
///   1. Compute per-frame RMS energy.
///   2. If RMS >= threshold → active (speech).
///   3. Hangover: stay in speech for N frames after last active frame.
///   4. Pre-buffer: include up to M frames before speech onset.
///   5. Minimum utterance: discard segments shorter than K frames (noise bursts).
use crate::buffer::RingBuffer;

const DEFAULT_SAMPLE_RATE: u64 = 16_000;

pub struct VadDetector {
    threshold: f32,
    hangover_frames: u32,
    hangover_counter: u32,
    min_speech_frames: u32,
    in_speech: bool,
    speech_frames: u32,
    speech_buffer: Vec<f32>,
    pre_buffer: RingBuffer,
    speech_ts_start: f64,
    frame_samples: usize,
}

#[allow(dead_code)]
pub enum VadEvent {
    Silence { rms: f32 },
    SpeechContinuing { rms: f32 },
    SpeechEnd {
        samples: Vec<f32>,
        duration_ms: u64,
        ts_start: f64,
        ts_end: f64,
        rms_peak: f32,
    },
}

impl VadDetector {
    /// Create a new detector.
    ///
    /// * `threshold`          – RMS level that counts as active speech (0.0–1.0, default 0.02)
    /// * `hangover_frames`    – How long to stay in speech after energy drops (default 10 = 300ms)
    /// * `pre_buffer_frames`  – Frames to prepend before detected onset (default 3 = 90ms)
    /// * `min_speech_frames`  – Minimum frames for a valid utterance (default 5 = 150ms)
    /// * `frame_samples`      – Samples per frame (480 for 30ms @ 16 kHz)
    pub fn new(
        threshold: f32,
        hangover_frames: u32,
        pre_buffer_frames: u32,
        min_speech_frames: u32,
        frame_samples: usize,
    ) -> Self {
        let pre_cap = (pre_buffer_frames as usize * frame_samples).max(1);
        Self {
            threshold: threshold.clamp(0.001, 1.0),
            hangover_frames,
            hangover_counter: 0,
            min_speech_frames,
            in_speech: false,
            speech_frames: 0,
            speech_buffer: Vec::new(),
            pre_buffer: RingBuffer::new(pre_cap),
            speech_ts_start: 0.0,
            frame_samples,
        }
    }

    pub fn set_threshold(&mut self, t: f32) {
        self.threshold = t.clamp(0.001, 1.0);
    }

    pub fn threshold(&self) -> f32 {
        self.threshold
    }

    pub fn is_in_speech(&self) -> bool {
        self.in_speech
    }

    /// Process one frame of audio.
    ///
    /// Returns `(rms, event)` where `event` reflects the VAD state change.
    pub fn process_frame(&mut self, samples: &[f32], ts: f64) -> (f32, VadEvent) {
        let rms = compute_rms(samples);
        let active = rms >= self.threshold;

        if active {
            self.hangover_counter = self.hangover_frames;
            if !self.in_speech {
                // Transition: silence → speech
                self.in_speech = true;
                self.speech_frames = 0;
                self.speech_ts_start = ts;
                // Include pre-buffered frames for natural onset
                self.speech_buffer = self.pre_buffer.drain_to_vec();
            }
            self.speech_buffer.extend_from_slice(samples);
            self.speech_frames += 1;
            (rms, VadEvent::SpeechContinuing { rms })
        } else if self.in_speech {
            if self.hangover_counter > 0 {
                // Still in hangover — remain in speech
                self.hangover_counter -= 1;
                self.speech_buffer.extend_from_slice(samples);
                (rms, VadEvent::SpeechContinuing { rms })
            } else {
                // Transition: speech → silence
                self.in_speech = false;
                let long_enough = self.speech_frames >= self.min_speech_frames;
                let utt_samples = std::mem::take(&mut self.speech_buffer);
                self.speech_frames = 0;

                // Seed pre-buffer with tail of last utterance
                let tail_start = utt_samples.len().saturating_sub(self.frame_samples);
                self.pre_buffer.push_slice(&utt_samples[tail_start..]);

                if long_enough {
                    let duration_ms =
                        (utt_samples.len() as u64 * 1000) / DEFAULT_SAMPLE_RATE;
                    let rms_peak = utt_samples
                        .chunks(self.frame_samples)
                        .map(compute_rms)
                        .fold(0.0_f32, f32::max);
                    (
                        rms,
                        VadEvent::SpeechEnd {
                            samples: utt_samples,
                            duration_ms,
                            ts_start: self.speech_ts_start,
                            ts_end: ts,
                            rms_peak,
                        },
                    )
                } else {
                    // Too short — treat as noise
                    (rms, VadEvent::Silence { rms })
                }
            }
        } else {
            // Silence: keep pre-buffer rolling
            self.pre_buffer.push_slice(samples);
            (rms, VadEvent::Silence { rms })
        }
    }
}

pub fn compute_rms(samples: &[f32]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }
    let sum_sq: f32 = samples.iter().map(|&s| s * s).sum();
    (sum_sq / samples.len() as f32).sqrt()
}

pub fn unix_ts() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_vad() -> VadDetector {
        VadDetector::new(0.02, 3, 2, 2, 8)
    }

    fn frame(rms_approx: f32, len: usize) -> Vec<f32> {
        vec![rms_approx; len]
    }

    #[test]
    fn test_silence_stays_silent() {
        let mut v = make_vad();
        let f = frame(0.01, 8);
        let (rms, event) = v.process_frame(&f, 0.0);
        assert!(rms < 0.02);
        assert!(!v.is_in_speech());
        assert!(matches!(event, VadEvent::Silence { .. }));
    }

    #[test]
    fn test_active_frame_enters_speech() {
        let mut v = make_vad();
        let f = frame(0.1, 8);
        v.process_frame(&f, 0.0);
        assert!(v.is_in_speech());
    }

    #[test]
    fn test_hangover_keeps_speech_alive() {
        let mut v = make_vad();
        v.process_frame(&frame(0.1, 8), 0.0); // speech start
        v.process_frame(&frame(0.01, 8), 0.1); // silence — hangover
        assert!(v.is_in_speech());
    }

    #[test]
    fn test_speech_end_after_hangover() {
        let mut v = VadDetector::new(0.02, 1, 2, 1, 8);
        v.process_frame(&frame(0.1, 8), 0.0);
        v.process_frame(&frame(0.01, 8), 0.1); // hangover (1 frame)
        let (_, event) = v.process_frame(&frame(0.01, 8), 0.2); // end
        assert!(!v.is_in_speech());
        assert!(matches!(event, VadEvent::SpeechEnd { .. }));
    }

    #[test]
    fn test_too_short_utterance_discarded() {
        // min_speech_frames = 5, but only 1 active frame
        let mut v = VadDetector::new(0.02, 0, 2, 5, 8);
        v.process_frame(&frame(0.1, 8), 0.0); // 1 active
        let (_, event) = v.process_frame(&frame(0.01, 8), 0.1); // end immediately
        assert!(matches!(event, VadEvent::Silence { .. }));
    }

    #[test]
    fn test_set_threshold() {
        let mut v = make_vad();
        v.set_threshold(0.05);
        assert!((v.threshold() - 0.05).abs() < 1e-6);
    }

    #[test]
    fn test_rms_calculation() {
        let samples = vec![0.1_f32; 100];
        let rms = compute_rms(&samples);
        assert!((rms - 0.1).abs() < 1e-5);
    }

    #[test]
    fn test_rms_empty() {
        assert_eq!(compute_rms(&[]), 0.0);
    }
}
