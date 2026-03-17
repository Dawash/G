/// G Voice Assistant — Rust audio capture + VAD binary.
///
/// Protocol (newline-delimited JSON):
///
///   stdin  ← commands from Python:
///     {"cmd": "start_capture"}
///     {"cmd": "stop_capture"}
///     {"cmd": "set_vad_threshold", "value": 0.02}
///     {"cmd": "get_status"}
///     {"cmd": "quit"}
///
///   stdout → events to Python:
///     {"type": "ready",     "sample_rate":16000, "frame_ms":30, "channels":1, "vad_threshold":0.02, "device":"..."}
///     {"type": "frame",     "ts":1234567890.123, "is_speech":true,  "rms":0.045}
///     {"type": "utterance", "ts_start":..., "ts_end":..., "duration_ms":333, "audio_b64":"...", "rms_peak":0.12}
///     {"type": "status",    "capturing":true, "vad_threshold":0.02, "device":"..."}
///     {"type": "error",     "msg":"..."}
mod buffer;
mod vad;

use std::io::{self, BufRead, Write};
use std::sync::{Arc, Mutex};
use std::sync::mpsc;
use std::time::Duration;

use base64::{engine::general_purpose::STANDARD as B64, Engine as _};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use serde::{Deserialize, Serialize};

// ── Constants ─────────────────────────────────────────────────────────────────

const FRAME_MS: u64 = 30;
const SAMPLE_RATE: u32 = 16_000;
/// Samples per 30ms frame at 16 kHz.
const FRAME_SAMPLES: usize = (SAMPLE_RATE as u64 * FRAME_MS / 1000) as usize; // 480

const DEFAULT_THRESHOLD: f32 = 0.020;
const HANGOVER_FRAMES: u32 = 10;   // 300ms
const PRE_BUFFER_FRAMES: u32 = 3;  //  90ms
const MIN_SPEECH_FRAMES: u32 = 5;  // 150ms minimum utterance

// ── Protocol types ────────────────────────────────────────────────────────────

#[derive(Deserialize, Debug)]
#[serde(tag = "cmd", rename_all = "snake_case")]
enum Command {
    StartCapture,
    StopCapture,
    SetVadThreshold { value: f32 },
    GetStatus,
    Quit,
}

#[derive(Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum Output {
    Ready {
        sample_rate: u32,
        frame_ms: u64,
        channels: u8,
        vad_threshold: f32,
        device: String,
    },
    Frame {
        ts: f64,
        is_speech: bool,
        rms: f32,
    },
    Utterance {
        ts_start: f64,
        ts_end: f64,
        duration_ms: u64,
        audio_b64: String,
        rms_peak: f32,
    },
    Status {
        capturing: bool,
        vad_threshold: f32,
        device: String,
    },
    Error {
        msg: String,
    },
}

enum AudioMsg {
    Samples(Vec<f32>),
    StreamError(String),
}

// ── Helpers ───────────────────────────────────────────────────────────────────

fn emit(out: &Output) {
    let line = serde_json::to_string(out).unwrap_or_else(|e| {
        format!(r#"{{"type":"error","msg":"serialize error: {}"}}"#, e)
    });
    let stdout = io::stdout();
    let mut lock = stdout.lock();
    let _ = writeln!(lock, "{}", line);
}

// ── Main ──────────────────────────────────────────────────────────────────────

fn main() {
    // Audio samples channel: cpal callback → main thread
    let (audio_tx, audio_rx) = mpsc::channel::<AudioMsg>();

    // ── Audio device setup ────────────────────────────────────────────────────
    let host = cpal::default_host();

    let device = match host.default_input_device() {
        Some(d) => d,
        None => {
            emit(&Output::Error {
                msg: "No input audio device found".to_string(),
            });
            return;
        }
    };

    let device_name = device.name().unwrap_or_else(|_| "unknown".to_string());

    let stream_config = cpal::StreamConfig {
        channels: 1,
        sample_rate: cpal::SampleRate(SAMPLE_RATE),
        buffer_size: cpal::BufferSize::Default,
    };

    // Shared flag: are we actively capturing?
    let capturing = Arc::new(Mutex::new(false));
    let capturing_cb = Arc::clone(&capturing);
    let audio_tx_cb = audio_tx.clone();
    let audio_tx_err = audio_tx;

    let stream_result = device.build_input_stream::<f32, _, _>(
        &stream_config,
        move |data: &[f32], _: &cpal::InputCallbackInfo| {
            if *capturing_cb.lock().unwrap() {
                let _ = audio_tx_cb.send(AudioMsg::Samples(data.to_vec()));
            }
        },
        move |err| {
            let _ = audio_tx_err.send(AudioMsg::StreamError(err.to_string()));
        },
        None, // no timeout
    );

    let stream = match stream_result {
        Ok(s) => s,
        Err(e) => {
            emit(&Output::Error {
                msg: format!("Failed to build audio stream: {}", e),
            });
            return;
        }
    };

    // ── Command channel: stdin reader thread → main thread ───────────────────
    let (cmd_tx, cmd_rx) = mpsc::channel::<Option<Command>>();

    std::thread::spawn(move || {
        let stdin = io::stdin();
        for line in stdin.lock().lines() {
            match line {
                Ok(l) => {
                    let trimmed = l.trim().to_string();
                    if trimmed.is_empty() {
                        continue;
                    }
                    match serde_json::from_str::<Command>(&trimmed) {
                        Ok(cmd) => {
                            let quit = matches!(cmd, Command::Quit);
                            let _ = cmd_tx.send(Some(cmd));
                            if quit {
                                break;
                            }
                        }
                        Err(e) => {
                            emit(&Output::Error {
                                msg: format!("Invalid command: {}", e),
                            });
                        }
                    }
                }
                Err(_) => {
                    let _ = cmd_tx.send(None); // EOF
                    break;
                }
            }
        }
    });

    // Signal readiness
    emit(&Output::Ready {
        sample_rate: SAMPLE_RATE,
        frame_ms: FRAME_MS,
        channels: 1,
        vad_threshold: DEFAULT_THRESHOLD,
        device: device_name.clone(),
    });

    // ── VAD and accumulation buffer ───────────────────────────────────────────
    let mut vad = vad::VadDetector::new(
        DEFAULT_THRESHOLD,
        HANGOVER_FRAMES,
        PRE_BUFFER_FRAMES,
        MIN_SPEECH_FRAMES,
        FRAME_SAMPLES,
    );
    let mut sample_accum: Vec<f32> = Vec::with_capacity(FRAME_SAMPLES * 8);
    let mut capturing_main = false;

    // ── Main processing loop ──────────────────────────────────────────────────
    loop {
        // Drain pending commands (non-blocking)
        loop {
            match cmd_rx.try_recv() {
                Ok(Some(Command::StartCapture)) => {
                    capturing_main = true;
                    *capturing.lock().unwrap() = true;
                    let _ = stream.play();
                }
                Ok(Some(Command::StopCapture)) => {
                    capturing_main = false;
                    *capturing.lock().unwrap() = false;
                    let _ = stream.pause();
                }
                Ok(Some(Command::SetVadThreshold { value })) => {
                    vad.set_threshold(value);
                }
                Ok(Some(Command::GetStatus)) => {
                    emit(&Output::Status {
                        capturing: capturing_main,
                        vad_threshold: vad.threshold(),
                        device: device_name.clone(),
                    });
                }
                Ok(Some(Command::Quit)) | Ok(None) => return,
                Err(mpsc::TryRecvError::Empty) => break,
                Err(mpsc::TryRecvError::Disconnected) => return,
            }
        }

        // Drain pending audio samples (non-blocking)
        let mut had_audio = false;
        loop {
            match audio_rx.try_recv() {
                Ok(AudioMsg::Samples(samples)) => {
                    had_audio = true;
                    sample_accum.extend_from_slice(&samples);

                    // Process complete 30ms frames
                    while sample_accum.len() >= FRAME_SAMPLES {
                        let frame: Vec<f32> =
                            sample_accum.drain(..FRAME_SAMPLES).collect();
                        let ts = vad::unix_ts();
                        let (rms, event) = vad.process_frame(&frame, ts);

                        emit(&Output::Frame {
                            ts,
                            is_speech: vad.is_in_speech(),
                            rms,
                        });

                        if let vad::VadEvent::SpeechEnd {
                            samples: utt_samples,
                            duration_ms,
                            ts_start,
                            ts_end,
                            rms_peak,
                        } = event
                        {
                            // Encode utterance as base64 f32-LE PCM
                            let bytes: Vec<u8> = utt_samples
                                .iter()
                                .flat_map(|&s| s.to_le_bytes())
                                .collect();
                            emit(&Output::Utterance {
                                ts_start,
                                ts_end,
                                duration_ms,
                                audio_b64: B64.encode(&bytes),
                                rms_peak,
                            });
                        }
                    }
                }
                Ok(AudioMsg::StreamError(e)) => {
                    emit(&Output::Error { msg: e });
                }
                Err(mpsc::TryRecvError::Empty) => break,
                Err(mpsc::TryRecvError::Disconnected) => return,
            }
        }

        if !had_audio {
            // Yield CPU when idle — 1ms keeps latency acceptable
            std::thread::sleep(Duration::from_millis(1));
        }
    }
}
