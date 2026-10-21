use std::collections::VecDeque;
use std::fs::{self, File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime};

use anyhow::{Context, Result, anyhow};
use chrono::{DateTime, Local};
use half::f16;
use image::ExtendedColorType;
use image::ImageEncoder;
use image::codecs::png::PngEncoder;
use img_hash::image::{ImageBuffer, Rgba, RgbaImage};
use img_hash::{HasherConfig, ImageHash};
use rusqlite::{Connection, params};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use tokio::runtime::Runtime;
use trayicon::{Icon, MenuBuilder, TrayIcon, TrayIconBuilder};
use windows_capture::dxgi_duplication_api::{DxgiDuplicationApi, DxgiDuplicationFormat};
use windows_capture::monitor::Monitor;
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
    MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, RegisterHotKey, UnregisterHotKey, VK_BACK, VK_DELETE,
    VK_DOWN, VK_END, VK_ESCAPE, VK_F1, VK_HOME, VK_INSERT, VK_LEFT, VK_NEXT, VK_PRIOR, VK_RETURN,
    VK_RIGHT, VK_SNAPSHOT, VK_SPACE, VK_TAB, VK_UP,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{GetMessageW, MSG, WM_HOTKEY};
use winit::application::ApplicationHandler;
use winit::event::WindowEvent;
use winit::event_loop::{ActiveEventLoop, EventLoop, EventLoopProxy};
use winit::window::Window;

#[derive(Clone, Debug, Eq, PartialEq)]
enum UserEvent {
    Start,
    Pause,
    Resume,
    CaptureNow,
    SetCadenceFixedInterval,
    SetCadenceAfterSourceReady,
    SetInterval(u64),
    SetOcrMaxSide(u64),
    ProcessNextJob,
    ProcessPendingQueue,
    AutoProcessNextJob,
    RestartApp,
    TooltipTick,
    OpenLogs,
    TailLogs,
    ToggleDebug,
    Quit,
    WorkerExited(WorkerLaunchMode),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ServiceState {
    Idle,
    Running,
    Paused,
    WorkerError,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum WorkerLaunchMode {
    Loop,
    Once,
    Drain,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CaptureLoopMode {
    Continuous,
    ManualBurst { restore_state: ServiceState },
}

#[derive(Clone, Debug, Deserialize)]
struct DaemonConfig {
    interval_seconds: u64,
    #[serde(default)]
    capture_after_source_ready: bool,
    #[serde(default)]
    manual_capture_hotkey: Option<String>,
    #[serde(default = "default_ocr_max_side")]
    ocr_max_side: u64,
    dedupe_window: usize,
    #[serde(default)]
    perceptual_dedupe_enabled: bool,
    phash_distance_threshold: u32,
    #[serde(default)]
    auto_start_worker: bool,
    #[serde(default = "default_true")]
    auto_process_jobs: bool,
    db_path: PathBuf,
    spool_dir: PathBuf,
    log_path: PathBuf,
    python_executable: String,
    python_args: Vec<String>,
    worker_script: PathBuf,
}

#[derive(Debug, Default, Deserialize)]
struct DaemonConfigPatch {
    interval_seconds: Option<u64>,
    capture_after_source_ready: Option<bool>,
    manual_capture_hotkey: Option<String>,
    ocr_max_side: Option<u64>,
    dedupe_window: Option<usize>,
    perceptual_dedupe_enabled: Option<bool>,
    phash_distance_threshold: Option<u32>,
    auto_start_worker: Option<bool>,
    auto_process_jobs: Option<bool>,
    db_path: Option<PathBuf>,
    spool_dir: Option<PathBuf>,
    log_path: Option<PathBuf>,
    python_executable: Option<String>,
    python_args: Option<Vec<String>>,
    worker_script: Option<PathBuf>,
}

fn jp_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("JP parent directory")
        .to_path_buf()
}

fn resolve_jp_config_path(value: PathBuf) -> PathBuf {
    if value.is_absolute() {
        value
    } else {
        jp_root().join(value)
    }
}

fn resolve_jp_command_path(value: String) -> String {
    let path = PathBuf::from(&value);
    if path.is_absolute() || path.components().count() <= 1 {
        value
    } else {
        jp_root().join(path).to_string_lossy().into_owned()
    }
}

fn repo_local_storage_path(path: &Path) -> String {
    if let Ok(relative) = path.strip_prefix(jp_root()) {
        relative.to_string_lossy().replace('\\', "/")
    } else {
        path.to_string_lossy().into_owned()
    }
}

impl Default for DaemonConfig {
    fn default() -> Self {
        let jp_root = jp_root();
        Self {
            interval_seconds: 60,
            capture_after_source_ready: false,
            manual_capture_hotkey: None,
            ocr_max_side: default_ocr_max_side(),
            dedupe_window: 12,
            perceptual_dedupe_enabled: false,
            phash_distance_threshold: 6,
            auto_start_worker: true,
            auto_process_jobs: true,
            db_path: jp_root.join("data").join("lang_jp.sqlite3"),
            spool_dir: jp_root.join("data").join("capture_spool"),
            log_path: jp_root.join("data").join("capture_daemon.log"),
            python_executable: "py".to_string(),
            python_args: vec!["-3.12".to_string()],
            worker_script: jp_root.join("capture_worker.py"),
        }
    }
}

fn default_true() -> bool {
    true
}

fn default_ocr_max_side() -> u64 {
    1600
}

impl DaemonConfig {
    fn from_toml(raw: &str) -> Result<Self> {
        let patch: DaemonConfigPatch = toml::from_str(raw)?;
        Ok(Self::default().apply_patch(patch))
    }

    fn apply_patch(mut self, patch: DaemonConfigPatch) -> Self {
        if let Some(value) = patch.interval_seconds {
            self.interval_seconds = value;
        }
        if let Some(value) = patch.capture_after_source_ready {
            self.capture_after_source_ready = value;
        }
        if let Some(value) = patch.manual_capture_hotkey {
            self.manual_capture_hotkey = Some(value);
        }
        if let Some(value) = patch.ocr_max_side {
            self.ocr_max_side = value;
        }
        if let Some(value) = patch.dedupe_window {
            self.dedupe_window = value;
        }
        if let Some(value) = patch.perceptual_dedupe_enabled {
            self.perceptual_dedupe_enabled = value;
        }
        if let Some(value) = patch.phash_distance_threshold {
            self.phash_distance_threshold = value;
        }
        if let Some(value) = patch.auto_start_worker {
            self.auto_start_worker = value;
        }
        if let Some(value) = patch.auto_process_jobs {
            self.auto_process_jobs = value;
        }
        if let Some(value) = patch.db_path {
            self.db_path = resolve_jp_config_path(value);
        }
        if let Some(value) = patch.spool_dir {
            self.spool_dir = resolve_jp_config_path(value);
        }
        if let Some(value) = patch.log_path {
            self.log_path = resolve_jp_config_path(value);
        }
        if let Some(value) = patch.python_executable {
            self.python_executable = resolve_jp_command_path(value);
        }
        if let Some(value) = patch.python_args {
            self.python_args = value;
        }
        if let Some(value) = patch.worker_script {
            self.worker_script = resolve_jp_config_path(value);
        }
        self
    }

    fn load() -> Self {
        let config_path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("capture_daemon.toml");
        let default = Self::default();
        let Ok(raw) = fs::read_to_string(&config_path) else {
            return default;
        };
        match Self::from_toml(&raw) {
            Ok(config) => config,
            Err(error) => {
                append_log_file(
                    &default.log_path,
                    &format!(
                        "failed to parse {}: {error}; using built-in defaults",
                        config_path.display()
                    ),
                );
                default
            }
        }
    }
}

#[derive(Clone, Debug)]
struct ManualCaptureHotkey {
    description: String,
    modifiers: u32,
    virtual_key: u32,
}

const MANUAL_CAPTURE_HOTKEY_ID: i32 = 0x4C4A;

fn parse_manual_capture_hotkey(raw: &str) -> Result<ManualCaptureHotkey> {
    let tokens: Vec<&str> = raw
        .split('+')
        .map(str::trim)
        .filter(|token| !token.is_empty())
        .collect();
    if tokens.is_empty() {
        return Err(anyhow!("hotkey is empty"));
    }

    let mut modifiers = 0u32;
    let mut key_token: Option<&str> = None;
    for token in tokens {
        let normalized = token.to_ascii_uppercase();
        match normalized.as_str() {
            "CTRL" | "CONTROL" => modifiers |= MOD_CONTROL,
            "ALT" => modifiers |= MOD_ALT,
            "SHIFT" => modifiers |= MOD_SHIFT,
            "WIN" | "WINDOWS" | "META" => modifiers |= MOD_WIN,
            _ => {
                if key_token.is_some() {
                    return Err(anyhow!("hotkey must contain exactly one non-modifier key"));
                }
                key_token = Some(token);
            }
        }
    }

    if modifiers == 0 {
        return Err(anyhow!("hotkey must include at least one modifier"));
    }

    let key_token = key_token.ok_or_else(|| anyhow!("hotkey is missing the final key"))?;
    let virtual_key = parse_manual_capture_hotkey_key(key_token)?;
    Ok(ManualCaptureHotkey {
        description: raw.trim().to_string(),
        modifiers,
        virtual_key,
    })
}

fn parse_manual_capture_hotkey_key(raw: &str) -> Result<u32> {
    let normalized = raw.trim().to_ascii_uppercase();
    if normalized.len() == 1 {
        let value = normalized.as_bytes()[0];
        if value.is_ascii_uppercase() || value.is_ascii_digit() {
            return Ok(u32::from(value));
        }
    }

    if let Some(function_number) = normalized.strip_prefix('F') {
        let number: u32 = function_number
            .parse()
            .map_err(|_| anyhow!("unsupported function key `{raw}`"))?;
        if (1..=24).contains(&number) {
            return Ok(u32::from(VK_F1) + (number - 1));
        }
    }

    match normalized.as_str() {
        "SPACE" => Ok(u32::from(VK_SPACE)),
        "TAB" => Ok(u32::from(VK_TAB)),
        "ESC" | "ESCAPE" => Ok(u32::from(VK_ESCAPE)),
        "ENTER" | "RETURN" => Ok(u32::from(VK_RETURN)),
        "BACKSPACE" => Ok(u32::from(VK_BACK)),
        "INSERT" => Ok(u32::from(VK_INSERT)),
        "DELETE" | "DEL" => Ok(u32::from(VK_DELETE)),
        "HOME" => Ok(u32::from(VK_HOME)),
        "END" => Ok(u32::from(VK_END)),
        "PAGEUP" | "PGUP" => Ok(u32::from(VK_PRIOR)),
        "PAGEDOWN" | "PGDN" => Ok(u32::from(VK_NEXT)),
        "LEFT" => Ok(u32::from(VK_LEFT)),
        "RIGHT" => Ok(u32::from(VK_RIGHT)),
        "UP" => Ok(u32::from(VK_UP)),
        "DOWN" => Ok(u32::from(VK_DOWN)),
        "PRINTSCREEN" | "PRTSC" => Ok(u32::from(VK_SNAPSHOT)),
        _ => Err(anyhow!("unsupported hotkey key `{raw}`")),
    }
}

fn spawn_manual_capture_hotkey_listener(config: &DaemonConfig, proxy: EventLoopProxy<UserEvent>) {
    let Some(raw_hotkey) = config
        .manual_capture_hotkey
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
    else {
        append_log_file(&config.log_path, "manual capture hotkey disabled");
        return;
    };

    let hotkey = match parse_manual_capture_hotkey(raw_hotkey) {
        Ok(hotkey) => hotkey,
        Err(error) => {
            append_log_file(
                &config.log_path,
                &format!("manual capture hotkey disabled because config is invalid: {error}"),
            );
            return;
        }
    };

    let log_path = config.log_path.clone();
    std::thread::spawn(move || unsafe {
        if RegisterHotKey(
            std::ptr::null_mut(),
            MANUAL_CAPTURE_HOTKEY_ID,
            hotkey.modifiers,
            hotkey.virtual_key,
        ) == 0
        {
            let error = std::io::Error::last_os_error();
            append_log_file(
                &log_path,
                &format!(
                    "failed to register manual capture hotkey `{}`: {}",
                    hotkey.description, error
                ),
            );
            return;
        }

        append_log_file(
            &log_path,
            &format!("manual capture hotkey registered `{}`", hotkey.description),
        );

        let mut message = std::mem::zeroed::<MSG>();
        loop {
            let result = GetMessageW(&mut message, std::ptr::null_mut(), 0, 0);
            if result == -1 {
                let error = std::io::Error::last_os_error();
                append_log_file(
                    &log_path,
                    &format!(
                        "manual capture hotkey listener stopped because GetMessageW failed: {}",
                        error
                    ),
                );
                break;
            }
            if result == 0 {
                break;
            }
            if message.message == WM_HOTKEY && message.wParam == MANUAL_CAPTURE_HOTKEY_ID as usize {
                let _ = proxy.send_event(UserEvent::CaptureNow);
            }
        }

        let _ = UnregisterHotKey(std::ptr::null_mut(), MANUAL_CAPTURE_HOTKEY_ID);
    });
}

struct SharedState {
    state: ServiceState,
    worker_child: Option<Child>,
    worker_mode: Option<WorkerLaunchMode>,
    debug_enabled: bool,
    capture_in_progress: bool,
    next_capture_at: Option<SystemTime>,
    pending_manual_capture: bool,
}

struct Controller {
    runtime: Runtime,
    config: DaemonConfig,
    interval_seconds: Arc<AtomicU64>,
    capture_after_source_ready: Arc<AtomicBool>,
    ocr_max_side: Arc<AtomicU64>,
    state: Arc<Mutex<SharedState>>,
    stop_flag: Arc<AtomicBool>,
    pause_flag: Arc<AtomicBool>,
    debug_flag: Arc<AtomicBool>,
}

impl Controller {
    fn new(config: DaemonConfig) -> Result<Self> {
        let initial_interval = config.interval_seconds;
        let initial_capture_after_source_ready = config.capture_after_source_ready;
        let initial_ocr_max_side = config.ocr_max_side;
        Ok(Self {
            runtime: Runtime::new().context("failed to create tokio runtime")?,
            config,
            interval_seconds: Arc::new(AtomicU64::new(initial_interval)),
            capture_after_source_ready: Arc::new(AtomicBool::new(
                initial_capture_after_source_ready,
            )),
            ocr_max_side: Arc::new(AtomicU64::new(initial_ocr_max_side)),
            state: Arc::new(Mutex::new(SharedState {
                state: ServiceState::Idle,
                worker_child: None,
                worker_mode: None,
                debug_enabled: false,
                capture_in_progress: false,
                next_capture_at: None,
                pending_manual_capture: false,
            })),
            stop_flag: Arc::new(AtomicBool::new(false)),
            pause_flag: Arc::new(AtomicBool::new(false)),
            debug_flag: Arc::new(AtomicBool::new(false)),
        })
    }

    fn state(&self) -> ServiceState {
        self.state.lock().unwrap().state
    }

    fn current_interval_seconds(&self) -> u64 {
        self.interval_seconds.load(Ordering::Relaxed)
    }

    fn current_ocr_max_side(&self) -> u64 {
        self.ocr_max_side.load(Ordering::Relaxed)
    }

    fn capture_after_source_ready_enabled(&self) -> bool {
        self.capture_after_source_ready.load(Ordering::Relaxed)
    }

    fn capture_cadence_label(&self) -> &'static str {
        if self.capture_after_source_ready_enabled() {
            "after-source"
        } else {
            "fixed"
        }
    }

    fn ocr_max_side_label(&self) -> String {
        match self.current_ocr_max_side() {
            0 => "original".to_string(),
            value => format!("{value}px"),
        }
    }

    fn set_state(&self, state: ServiceState) {
        self.state.lock().unwrap().state = state;
    }

    fn set_capture_schedule(&self, in_progress: bool, next_capture_at: Option<SystemTime>) {
        let mut guard = self.state.lock().unwrap();
        guard.capture_in_progress = in_progress;
        guard.next_capture_at = next_capture_at;
        if guard.state != ServiceState::Running {
            guard.pending_manual_capture = false;
        }
    }

    fn tooltip_details(&self) -> String {
        let guard = self.state.lock().unwrap();
        if guard.state != ServiceState::Running {
            return match guard.state {
                ServiceState::Idle => format!(
                    "Idle | cadence {} | interval {}s | ocr {}",
                    self.capture_cadence_label(),
                    self.current_interval_seconds(),
                    self.ocr_max_side_label()
                ),
                ServiceState::Paused => {
                    format!(
                        "Paused | cadence {} | interval {}s | ocr {}",
                        self.capture_cadence_label(),
                        self.current_interval_seconds(),
                        self.ocr_max_side_label()
                    )
                }
                ServiceState::WorkerError => {
                    format!(
                        "Worker error | cadence {} | interval {}s | ocr {}",
                        self.capture_cadence_label(),
                        self.current_interval_seconds(),
                        self.ocr_max_side_label()
                    )
                }
                ServiceState::Running => "Running".to_string(),
            };
        }
        if guard.capture_in_progress {
            return format!(
                "Running | capturing now | cadence {} | interval {}s | ocr {}",
                self.capture_cadence_label(),
                self.current_interval_seconds(),
                self.ocr_max_side_label()
            );
        }
        if let Some(deadline) = guard.next_capture_at {
            let remaining = deadline
                .duration_since(SystemTime::now())
                .unwrap_or(Duration::ZERO)
                .as_secs();
            return format!(
                "Running | next capture in {remaining}s | cadence {} | interval {}s | ocr {}",
                self.capture_cadence_label(),
                self.current_interval_seconds(),
                self.ocr_max_side_label()
            );
        }
        if self.capture_after_source_ready_enabled() {
            return format!(
                "Running | waiting for source | cadence {} | ocr {}",
                self.capture_cadence_label(),
                self.ocr_max_side_label()
            );
        }
        format!(
            "Running | cadence {} | interval {}s | ocr {}",
            self.capture_cadence_label(),
            self.current_interval_seconds(),
            self.ocr_max_side_label()
        )
    }

    fn debug_enabled(&self) -> bool {
        self.debug_flag.load(Ordering::Relaxed)
    }

    fn toggle_debug(&self) {
        let enabled = !self.debug_flag.load(Ordering::Relaxed);
        self.debug_flag.store(enabled, Ordering::Relaxed);
        self.state.lock().unwrap().debug_enabled = enabled;
        let mode = if enabled { "enabled" } else { "disabled" };
        self.append_log(&format!("debug mode {mode}"));
    }

    fn start(&self, proxy: EventLoopProxy<UserEvent>) {
        if self.state() == ServiceState::Running {
            self.append_log("start requested while capture service was already running");
            return;
        }
        self.stop_flag.store(false, Ordering::Relaxed);
        self.pause_flag.store(false, Ordering::Relaxed);
        self.set_state(ServiceState::Running);
        self.set_capture_schedule(true, None);
        self.append_log(&format!(
            "capture service started cadence={} interval={}s ocr_max_side={} dedupe_window={} perceptual_dedupe={} phash_threshold={} auto_start_worker={} auto_process_jobs={} debug={}",
            self.capture_cadence_label(),
            self.current_interval_seconds(),
            self.ocr_max_side_label(),
            self.config.dedupe_window,
            self.config.perceptual_dedupe_enabled,
            self.config.phash_distance_threshold,
            self.config.auto_start_worker,
            self.config.auto_process_jobs,
            self.debug_enabled()
        ));
        if self.config.auto_start_worker {
            self.spawn_worker(WorkerLaunchMode::Loop, Some(proxy.clone()));
        } else if self.config.auto_process_jobs {
            self.append_log(
                "automatic queue processing enabled; capture jobs will be processed one-at-a-time",
            );
            let _ = proxy.send_event(UserEvent::AutoProcessNextJob);
        } else {
            self.append_log("automatic OCR worker disabled; capture jobs will stay pending until processed manually");
        }
        self.spawn_capture_loop(proxy, CaptureLoopMode::Continuous);
    }

    fn pause(&self) {
        self.pause_flag.store(true, Ordering::Relaxed);
        self.set_capture_schedule(false, None);
        self.set_state(ServiceState::Paused);
        self.append_log("capture service paused");
    }

    fn resume(&self, proxy: EventLoopProxy<UserEvent>) {
        self.pause_flag.store(false, Ordering::Relaxed);
        self.set_capture_schedule(true, None);
        self.set_state(ServiceState::Running);
        self.append_log("capture service resumed");
        if self.config.auto_start_worker && self.state.lock().unwrap().worker_child.is_none() {
            self.spawn_worker(WorkerLaunchMode::Loop, Some(proxy));
        } else if self.config.auto_process_jobs {
            let _ = proxy.send_event(UserEvent::AutoProcessNextJob);
        }
    }

    fn capture_now(&self, proxy: EventLoopProxy<UserEvent>) {
        let state = self.state();
        if state != ServiceState::Running {
            self.stop_flag.store(false, Ordering::Relaxed);
            self.pause_flag.store(false, Ordering::Relaxed);
            {
                let mut guard = self.state.lock().unwrap();
                guard.state = ServiceState::Running;
                guard.capture_in_progress = false;
                guard.next_capture_at = Some(SystemTime::now());
                guard.pending_manual_capture = false;
            }
            self.append_log(&format!(
                "manual capture requested while capture service was {state:?}; starting on-demand capture"
            ));
            self.spawn_capture_loop(proxy, CaptureLoopMode::ManualBurst { restore_state: state });
            return;
        }

        let mut guard = self.state.lock().unwrap();
        if guard.capture_in_progress {
            if guard.pending_manual_capture {
                self.append_log(
                    "manual capture request ignored because an immediate follow-up capture is already pending",
                );
                return;
            }
            guard.pending_manual_capture = true;
            self.append_log(
                "manual capture requested while a capture was in progress; scheduling an immediate follow-up capture",
            );
            return;
        }

        guard.pending_manual_capture = false;
        guard.next_capture_at = Some(SystemTime::now());
        self.append_log(
            "manual capture requested; next capture scheduled immediately and timer reset",
        );
    }

    fn set_interval_seconds(&self, seconds: u64) {
        self.interval_seconds.store(seconds, Ordering::Relaxed);
        {
            let mut guard = self.state.lock().unwrap();
            if guard.state == ServiceState::Running
                && !guard.capture_in_progress
                && !self.capture_after_source_ready_enabled()
            {
                guard.next_capture_at = SystemTime::now().checked_add(Duration::from_secs(seconds));
            }
        }
        self.append_log(&format!("capture interval updated to {}s", seconds));
    }

    fn set_capture_after_source_ready(&self, enabled: bool) {
        self.capture_after_source_ready
            .store(enabled, Ordering::Relaxed);
        {
            let mut guard = self.state.lock().unwrap();
            if guard.state == ServiceState::Running && !guard.capture_in_progress {
                if enabled {
                    guard.next_capture_at = None;
                } else {
                    guard.next_capture_at = SystemTime::now()
                        .checked_add(Duration::from_secs(self.current_interval_seconds()));
                }
            }
        }
        self.append_log(&format!(
            "capture cadence updated to {}",
            self.capture_cadence_label()
        ));
        if enabled && self.config.auto_start_worker {
            self.append_log(
                "capture cadence after-source is most accurate with auto_start_worker=false",
            );
        }
        if enabled && !self.config.auto_process_jobs {
            self.append_log(
                "capture cadence after-source requires manual OCR processing when auto_process_jobs=false",
            );
        }
    }

    fn set_ocr_max_side(&self, value: u64) {
        self.ocr_max_side.store(value, Ordering::Relaxed);
        self.append_log(&format!(
            "ocr max side updated to {}",
            self.ocr_max_side_label()
        ));
    }

    fn stop(&self) {
        self.stop_flag.store(true, Ordering::Relaxed);
        self.pause_flag.store(false, Ordering::Relaxed);
        self.kill_worker();
        self.set_capture_schedule(false, None);
        self.set_state(ServiceState::Idle);
        self.append_log("capture service stopped");
    }

    fn open_logs(&self) {
        let log_path = self.config.log_path.clone();
        let _ = Command::new("cmd")
            .args(["/C", "start", "", log_path.to_string_lossy().as_ref()])
            .spawn();
    }

    fn tail_logs(&self) {
        let escaped = self.config.log_path.to_string_lossy().replace('\'', "''");
        let command = format!("Get-Content -Path '{}' -Tail 80 -Wait", escaped);
        let _ = Command::new("powershell")
            .args(["-NoExit", "-Command", &command])
            .spawn();
    }

    fn append_log(&self, message: &str) {
        let timestamp = Local::now().format("%Y-%m-%d %H:%M:%S");
        if let Some(parent) = self.config.log_path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        if let Ok(mut file) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.config.log_path)
        {
            let _ = writeln!(file, "[{}] {}", timestamp, message);
        }
    }

    fn spawn_worker(&self, mode: WorkerLaunchMode, proxy: Option<EventLoopProxy<UserEvent>>) {
        if self.state.lock().unwrap().worker_child.is_some() {
            self.append_log("worker launch skipped because another OCR worker is already running");
            return;
        }
        let mut command = Command::new(&self.config.python_executable);
        command.args(&self.config.python_args);
        command.arg(&self.config.worker_script);
        command.arg("--db");
        command.arg(&self.config.db_path);
        command.env(
            "LANG_JP_OCR_MAX_SIDE",
            self.current_ocr_max_side().to_string(),
        );
        command.arg(match mode {
            WorkerLaunchMode::Loop => "loop",
            WorkerLaunchMode::Once => "once",
            WorkerLaunchMode::Drain => "drain",
        });
        if self.debug_enabled() {
            command.env("LANG_JP_CAPTURE_DEBUG", "1");
        }

        match command.spawn() {
            Ok(child) => {
                {
                    let mut guard = self.state.lock().unwrap();
                    guard.worker_child = Some(child);
                    guard.worker_mode = Some(mode);
                }
                self.append_log(&format!("spawned capture worker mode={mode:?}"));
                let state = Arc::clone(&self.state);
                let log_path = self.config.log_path.clone();
                std::thread::spawn(move || {
                    loop {
                        std::thread::sleep(Duration::from_secs(1));
                        let mut guard = state.lock().unwrap();
                        let Some(child) = guard.worker_child.as_mut() else {
                            break;
                        };
                        match child.try_wait() {
                            Ok(Some(status)) => {
                                let exited_mode = guard.worker_mode.take();
                                let timestamp = Local::now().format("%Y-%m-%d %H:%M:%S");
                                if let Ok(mut file) =
                                    OpenOptions::new().create(true).append(true).open(&log_path)
                                {
                                    let _ = writeln!(
                                        file,
                                        "[{}] capture worker exited mode={:?} status={}",
                                        timestamp, exited_mode, status
                                    );
                                }
                                guard.worker_child = None;
                                if let Some(mode) = exited_mode {
                                    if let Some(proxy) = proxy.clone() {
                                        let _ = proxy.send_event(UserEvent::WorkerExited(mode));
                                    }
                                }
                                break;
                            }
                            Ok(None) => {}
                            Err(_) => {
                                let exited_mode = guard.worker_mode.take();
                                guard.worker_child = None;
                                if let Some(mode) = exited_mode {
                                    if let Some(proxy) = proxy.clone() {
                                        let _ = proxy.send_event(UserEvent::WorkerExited(mode));
                                    }
                                }
                                break;
                            }
                        }
                    }
                });
            }
            Err(error) => {
                self.append_log(&format!("failed to spawn capture worker: {error}"));
                self.set_state(ServiceState::WorkerError);
            }
        }
    }

    fn process_next_job(&self) {
        self.append_log("manual OCR requested for next pending capture job");
        self.spawn_worker(WorkerLaunchMode::Once, None);
    }

    fn process_pending_queue(&self) {
        self.append_log("manual OCR requested for all pending capture jobs");
        self.spawn_worker(WorkerLaunchMode::Drain, None);
    }

    fn auto_process_next_job(&self) {
        if self.config.auto_start_worker || !self.config.auto_process_jobs {
            return;
        }
        if self.state.lock().unwrap().worker_child.is_some() {
            return;
        }
        if !db_has_pending_capture_jobs(&self.config.db_path) {
            return;
        }
        self.append_log("automatic OCR requested for next pending capture job");
        self.spawn_worker(WorkerLaunchMode::Once, None);
    }

    fn restart_app(&self) -> Result<()> {
        let current_exe = std::env::current_exe().context("failed to locate current executable")?;
        Command::new(&current_exe)
            .spawn()
            .with_context(|| format!("failed to relaunch {}", current_exe.display()))?;
        self.append_log(&format!(
            "app restart requested via {}",
            current_exe.display()
        ));
        Ok(())
    }

    fn kill_worker(&self) {
        let child = {
            let mut guard = self.state.lock().unwrap();
            guard.worker_mode = None;
            guard.worker_child.take()
        };
        if let Some(mut child) = child {
            let _ = child.kill();
        }
    }

    fn spawn_capture_loop(&self, proxy: EventLoopProxy<UserEvent>, mode: CaptureLoopMode) {
        let config = self.config.clone();
        let interval_seconds = Arc::clone(&self.interval_seconds);
        let capture_after_source_ready = Arc::clone(&self.capture_after_source_ready);
        let stop_flag = Arc::clone(&self.stop_flag);
        let pause_flag = Arc::clone(&self.pause_flag);
        let debug_flag = Arc::clone(&self.debug_flag);
        let shared_state = Arc::clone(&self.state);
        let log_path = self.config.log_path.clone();
        let queue_proxy = proxy.clone();
        self.runtime.spawn_blocking(move || {
            append_log_file(
                &log_path,
                &format!(
                    "capture loop initialized mode={:?} cadence={} interval={}s dedupe_window={} perceptual_dedupe={} phash_threshold={} debug={}",
                    mode,
                    if capture_after_source_ready.load(Ordering::Relaxed) {
                        "after-source"
                    } else {
                        "fixed"
                    },
                    interval_seconds.load(Ordering::Relaxed),
                    config.dedupe_window,
                    config.perceptual_dedupe_enabled,
                    config.phash_distance_threshold,
                    debug_flag.load(Ordering::Relaxed)
                ),
            );
            let monitor = match Monitor::primary() {
                Ok(monitor) => monitor,
                Err(error) => {
                    append_log_file(&log_path, &format!("failed to get primary monitor: {error}"));
                    return;
                }
            };
            let mut capture = match DxgiDuplicationApi::new_options(
                monitor,
                &[DxgiDuplicationFormat::Bgra8, DxgiDuplicationFormat::Rgba8],
            ) {
                Ok(capture) => capture,
                Err(error) => {
                    append_log_file(
                        &log_path,
                        &format!("failed to initialize DXGI duplication: {error}"),
                    );
                    return;
                }
            };
            let mut recent: VecDeque<CaptureFingerprint> = VecDeque::new();
            let hasher = HasherConfig::new().hash_size(8, 8).to_hasher();

            while !stop_flag.load(Ordering::Relaxed) {
                if pause_flag.load(Ordering::Relaxed) {
                {
                    let mut guard = shared_state.lock().unwrap();
                    guard.capture_in_progress = false;
                    guard.next_capture_at = None;
                    guard.pending_manual_capture = false;
                }
                std::thread::sleep(Duration::from_millis(300));
                continue;
                }

                {
                    let mut guard = shared_state.lock().unwrap();
                    guard.capture_in_progress = true;
                    guard.next_capture_at = None;
                }

                if debug_flag.load(Ordering::Relaxed) {
                    append_log_file(&log_path, "capture tick");
                }

                let tick_outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    match capture.acquire_next_frame(33) {
                    Ok(mut frame) => {
                        let frame_buffer = match frame.buffer() {
                            Ok(buffer) => buffer,
                            Err(error) => {
                                append_log_file(
                                    &log_path,
                                    &format!("failed to read frame buffer: {error}"),
                                );
                                return;
                            }
                        };
                        let width = frame_buffer.width();
                        let height = frame_buffer.height();
                        let color_format = frame_buffer.format();
                        let mut nopadding = Vec::new();
                        let buffer = frame_buffer.as_nopadding_buffer(&mut nopadding).to_vec();
                        let sha256 = sha256_hex(&buffer);
                        let image = match rgba_image_from_raw(width, height, buffer, color_format) {
                            Ok(image) => image,
                            Err(error) => {
                                append_log_file(
                                    &log_path,
                                    &format!("failed to decode captured frame: {error}"),
                                );
                                return;
                            }
                        };
                        let phash = hasher.hash_image(&image);
                        if debug_flag.load(Ordering::Relaxed) {
                            append_log_file(
                                &log_path,
                                &format!(
                                    "captured frame {}x{} format={:?} sha={}",
                                    width, height, color_format, sha256
                                ),
                            );
                        }
                        let skip_reason = should_skip_capture(
                            &config,
                            &sha256,
                            &phash,
                            &recent,
                            &config.db_path,
                        );
                        if let Some(reason) = skip_reason {
                            append_log_file(
                                &log_path,
                                &format!("skip capture sha={} reason={reason}", short_hash(&sha256)),
                            );
                            return;
                        }

                        if debug_flag.load(Ordering::Relaxed) {
                            append_log_file(
                                &log_path,
                                &format!(
                                    "capture accepted for persistence sha={} format={:?}",
                                    sha256, color_format
                                ),
                            );
                        }

                        let temp_path = build_spool_path(&config.spool_dir, &sha256);
                        if let Some(parent) = temp_path.parent() {
                            let _ = fs::create_dir_all(parent);
                        }
                        if let Err(error) = save_rgba_png(&image, &temp_path) {
                            append_log_file(
                                &log_path,
                                &format!("failed to save spool image: {error}"),
                            );
                            return;
                        }

                        if debug_flag.load(Ordering::Relaxed) {
                            append_log_file(
                                &log_path,
                                &format!("spool image={} saved", short_path(&temp_path)),
                            );
                        }

                        match enqueue_capture_job(
                            &config.db_path,
                            &temp_path,
                            &sha256,
                            &phash,
                        ) {
                            Ok(job_id) => {
                                append_log_file(
                                    &log_path,
                                    &format!(
                                        "queue job={job_id} image={} sha={}",
                                        short_path(&temp_path),
                                        short_hash(&sha256),
                                    ),
                                );
                                recent.push_back(CaptureFingerprint { sha256, phash });
                                while recent.len() > config.dedupe_window {
                                    recent.pop_front();
                                }
                                if config.auto_process_jobs
                                    && (!config.auto_start_worker
                                        || matches!(mode, CaptureLoopMode::ManualBurst { .. }))
                                {
                                    let _ = queue_proxy.send_event(UserEvent::AutoProcessNextJob);
                                }
                            }
                            Err(error) => {
                                append_log_file(
                                    &log_path,
                                    &format!("failed to enqueue capture job: {error}"),
                                );
                                let _ = fs::remove_file(&temp_path);
                            }
                        }
                    }
                    Err(error) => {
                        append_log_file(
                            &log_path,
                            &format!("failed to acquire frame: {error}"),
                        );
                    }
                }
                }));

                if let Err(payload) = tick_outcome {
                    append_log_file(
                        &log_path,
                        &format!("capture loop panicked: {}", panic_payload_message(payload)),
                    );
                }

                let current_interval_seconds = interval_seconds.load(Ordering::Relaxed);
                let mut follow_up_capture_requested = false;
                let use_after_source_cadence =
                    capture_after_source_ready.load(Ordering::Relaxed);
                {
                    let mut guard = shared_state.lock().unwrap();
                    guard.capture_in_progress = false;
                    if guard.pending_manual_capture {
                        guard.pending_manual_capture = false;
                        guard.next_capture_at = Some(SystemTime::now());
                        follow_up_capture_requested = true;
                    } else if use_after_source_cadence {
                        guard.next_capture_at = None;
                    } else {
                        guard.next_capture_at = SystemTime::now()
                            .checked_add(Duration::from_secs(current_interval_seconds));
                    }
                }
                if follow_up_capture_requested {
                    append_log_file(
                        &log_path,
                        "schedule next=now reason=manual_follow_up",
                    );
                } else {
                    if use_after_source_cadence {
                        append_log_file(
                            &log_path,
                            "schedule next=source_ready",
                        );
                    } else {
                        append_log_file(
                            &log_path,
                            &format!("schedule next={}s", current_interval_seconds),
                        );
                    }
                }
                if let CaptureLoopMode::ManualBurst { restore_state } = mode {
                    if !follow_up_capture_requested {
                        {
                            let mut guard = shared_state.lock().unwrap();
                            guard.state = restore_state;
                            guard.capture_in_progress = false;
                            guard.next_capture_at = None;
                            guard.pending_manual_capture = false;
                        }
                        append_log_file(
                            &log_path,
                            &format!(
                                "manual capture burst completed; restored service state to {restore_state:?}"
                            ),
                        );
                        break;
                    }
                }
                loop {
                    if stop_flag.load(Ordering::Relaxed) || pause_flag.load(Ordering::Relaxed) {
                        break;
                    }
                    let deadline = { shared_state.lock().unwrap().next_capture_at };
                    if let Some(deadline) = deadline {
                        let now = SystemTime::now();
                        if now >= deadline {
                            break;
                        }
                        let remaining = deadline
                            .duration_since(now)
                            .unwrap_or(Duration::ZERO);
                        std::thread::sleep(remaining.min(Duration::from_millis(250)));
                        continue;
                    }
                    if capture_after_source_ready.load(Ordering::Relaxed)
                        && capture_pipeline_ready_for_next_capture(&config.db_path, &shared_state)
                    {
                        append_log_file(
                            &log_path,
                            "schedule next=now reason=source_ready",
                        );
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(250));
                }
            }
        });
    }
}

#[derive(Clone)]
struct CaptureFingerprint {
    sha256: String,
    phash: ImageHash,
}

struct App {
    window: Option<Window>,
    tray_icon: TrayIcon<UserEvent>,
    controller: Arc<Controller>,
    proxy: EventLoopProxy<UserEvent>,
}

fn capture_now_menu_label(config: &DaemonConfig) -> String {
    match config
        .manual_capture_hotkey
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        Some(hotkey) => format!("Capture now [{hotkey}]"),
        None => "Capture now".to_string(),
    }
}

impl App {
    fn refresh_tooltip(&mut self) {
        let tooltip = format!("Lang JP Capture: {}", self.controller.tooltip_details());
        let decorated = if self.controller.debug_enabled() {
            format!("{tooltip} [debug]")
        } else {
            tooltip
        };
        let _ = self.tray_icon.set_tooltip(&decorated);
    }
}

impl ApplicationHandler<UserEvent> for App {
    fn resumed(&mut self, event_loop: &ActiveEventLoop) {
        self.window = Some(
            event_loop
                .create_window(Window::default_attributes().with_visible(false))
                .expect("create hidden window"),
        );
        self.refresh_tooltip();
    }

    fn user_event(&mut self, event_loop: &ActiveEventLoop, event: UserEvent) {
        match event {
            UserEvent::Start => self.controller.start(self.proxy.clone()),
            UserEvent::Pause => self.controller.pause(),
            UserEvent::Resume => self.controller.resume(self.proxy.clone()),
            UserEvent::CaptureNow => self.controller.capture_now(self.proxy.clone()),
            UserEvent::SetCadenceFixedInterval => {
                self.controller.set_capture_after_source_ready(false)
            }
            UserEvent::SetCadenceAfterSourceReady => {
                self.controller.set_capture_after_source_ready(true)
            }
            UserEvent::SetInterval(seconds) => self.controller.set_interval_seconds(seconds),
            UserEvent::SetOcrMaxSide(value) => self.controller.set_ocr_max_side(value),
            UserEvent::ProcessNextJob => self.controller.process_next_job(),
            UserEvent::ProcessPendingQueue => self.controller.process_pending_queue(),
            UserEvent::AutoProcessNextJob => self.controller.auto_process_next_job(),
            UserEvent::RestartApp => match self.controller.restart_app() {
                Ok(()) => {
                    self.controller.stop();
                    event_loop.exit();
                }
                Err(error) => self
                    .controller
                    .append_log(&format!("failed to restart app: {error}")),
            },
            UserEvent::TooltipTick => {}
            UserEvent::OpenLogs => self.controller.open_logs(),
            UserEvent::TailLogs => self.controller.tail_logs(),
            UserEvent::ToggleDebug => self.controller.toggle_debug(),
            UserEvent::Quit => {
                self.controller.stop();
                event_loop.exit();
            }
            UserEvent::WorkerExited(mode) => match mode {
                WorkerLaunchMode::Loop => self.controller.set_state(ServiceState::WorkerError),
                WorkerLaunchMode::Drain => {}
                WorkerLaunchMode::Once => self.controller.auto_process_next_job(),
            },
        }
        self.refresh_tooltip();
    }

    fn window_event(
        &mut self,
        event_loop: &ActiveEventLoop,
        _window_id: winit::window::WindowId,
        event: WindowEvent,
    ) {
        if let WindowEvent::CloseRequested = event {
            self.controller.stop();
            event_loop.exit();
        }
    }
}

fn build_tray_icon(
    proxy: EventLoopProxy<UserEvent>,
    config: &DaemonConfig,
) -> Result<TrayIcon<UserEvent>> {
    let icon = Icon::from_buffer(build_tray_icon_ico(), Some(16), Some(16))
        .map_err(|error| anyhow!("failed to create tray icon: {error}"))?;
    let tray_icon = TrayIconBuilder::new()
        .sender(move |event: &UserEvent| {
            let _ = proxy.send_event(event.clone());
        })
        .icon(icon)
        .tooltip("Lang JP Capture")
        .menu(
            MenuBuilder::new()
                .item("Start", UserEvent::Start)
                .item("Pause", UserEvent::Pause)
                .item("Resume", UserEvent::Resume)
                .item(&capture_now_menu_label(config), UserEvent::CaptureNow)
                .separator()
                .item(
                    "Cadence: Fixed interval",
                    UserEvent::SetCadenceFixedInterval,
                )
                .item(
                    "Cadence: After source ready",
                    UserEvent::SetCadenceAfterSourceReady,
                )
                .separator()
                .item("Capture interval: 5s", UserEvent::SetInterval(5))
                .item("Capture interval: 15s", UserEvent::SetInterval(15))
                .item("Capture interval: 30s", UserEvent::SetInterval(30))
                .item("Capture interval: 60s", UserEvent::SetInterval(60))
                .item("Capture interval: 120s", UserEvent::SetInterval(120))
                .separator()
                .item("OCR size: Original (4K)", UserEvent::SetOcrMaxSide(0))
                .item("OCR size: 1600px", UserEvent::SetOcrMaxSide(1600))
                .item("OCR size: 1920px", UserEvent::SetOcrMaxSide(1920))
                .item("OCR size: 2560px", UserEvent::SetOcrMaxSide(2560))
                .separator()
                .item("Process next OCR job", UserEvent::ProcessNextJob)
                .item(
                    "Process all pending OCR jobs",
                    UserEvent::ProcessPendingQueue,
                )
                .item("Restart app", UserEvent::RestartApp)
                .separator()
                .item("Open logs", UserEvent::OpenLogs)
                .item("Tail logs", UserEvent::TailLogs)
                .item("Toggle debug mode", UserEvent::ToggleDebug)
                .separator()
                .item("Quit", UserEvent::Quit),
        )
        .build()
        .map_err(|error| anyhow!("failed to build tray icon: {error}"))?;
    Ok(tray_icon)
}

fn build_tray_icon_ico() -> &'static [u8] {
    let width = 16u8;
    let height = 16u8;
    let xor_pixels_len = 16usize * 16usize * 4usize;
    let and_mask_row_len = 4usize;
    let and_mask_len = and_mask_row_len * 16usize;
    let bitmap_len = 40usize + xor_pixels_len + and_mask_len;
    let mut bytes = Vec::with_capacity(22usize + bitmap_len);

    bytes.extend_from_slice(&0u16.to_le_bytes());
    bytes.extend_from_slice(&1u16.to_le_bytes());
    bytes.extend_from_slice(&1u16.to_le_bytes());

    bytes.push(width);
    bytes.push(height);
    bytes.push(0);
    bytes.push(0);
    bytes.extend_from_slice(&1u16.to_le_bytes());
    bytes.extend_from_slice(&32u16.to_le_bytes());
    bytes.extend_from_slice(&(bitmap_len as u32).to_le_bytes());
    bytes.extend_from_slice(&22u32.to_le_bytes());

    bytes.extend_from_slice(&40u32.to_le_bytes());
    bytes.extend_from_slice(&(16i32).to_le_bytes());
    bytes.extend_from_slice(&(32i32).to_le_bytes());
    bytes.extend_from_slice(&1u16.to_le_bytes());
    bytes.extend_from_slice(&32u16.to_le_bytes());
    bytes.extend_from_slice(&0u32.to_le_bytes());
    bytes.extend_from_slice(&((xor_pixels_len + and_mask_len) as u32).to_le_bytes());
    bytes.extend_from_slice(&0i32.to_le_bytes());
    bytes.extend_from_slice(&0i32.to_le_bytes());
    bytes.extend_from_slice(&0u32.to_le_bytes());
    bytes.extend_from_slice(&0u32.to_le_bytes());

    for y in (0usize..16usize).rev() {
        for x in 0usize..16usize {
            let border = x == 0 || x == 15 || y == 0 || y == 15;
            let accent = x == y || x + y == 15;
            let bgra = if border {
                [0x22, 0x22, 0x22, 0xff]
            } else if accent {
                [0x33, 0x5c, 0xb2, 0xff]
            } else {
                [0xc8, 0xe0, 0xf3, 0xff]
            };
            bytes.extend_from_slice(&bgra);
        }
    }
    bytes.extend(std::iter::repeat_n(0u8, and_mask_len));
    Box::leak(bytes.into_boxed_slice())
}

fn open_db(path: &Path) -> Result<Connection> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).ok();
    }
    let connection = Connection::open(path)?;
    connection.execute_batch(
        "
        PRAGMA journal_mode = WAL;
        PRAGMA busy_timeout = 30000;
        PRAGMA foreign_keys = ON;
        ",
    )?;
    Ok(connection)
}

fn enqueue_capture_job(
    db_path: &Path,
    temp_path: &Path,
    sha256: &str,
    phash: &ImageHash,
) -> Result<i64> {
    let connection = open_db(db_path)?;
    let now: DateTime<Local> = Local::now();
    let temp_image_path = repo_local_storage_path(temp_path);
    connection.execute(
        "
        INSERT INTO capture_jobs (
            captured_at,
            status,
            temp_image_path,
            image_sha256,
            image_phash
        )
        VALUES (?1, 'pending', ?2, ?3, ?4)
        ",
        params![
            now.to_rfc3339(),
            temp_image_path,
            sha256,
            phash.to_base64(),
        ],
    )?;
    Ok(connection.last_insert_rowid())
}

fn db_has_capture_hash(db_path: &Path, sha256: &str) -> bool {
    let Ok(connection) = open_db(db_path) else {
        return false;
    };
    let source_exists = connection
        .query_row(
            "SELECT 1 FROM sources WHERE image_sha256 = ?1 LIMIT 1",
            params![sha256],
            |_| Ok(()),
        )
        .is_ok();
    if source_exists {
        return true;
    }
    connection
        .query_row(
            "
            SELECT 1
            FROM capture_jobs
            WHERE image_sha256 = ?1
              AND status IN ('pending', 'processing', 'accepted')
            LIMIT 1
            ",
            params![sha256],
            |_| Ok(()),
        )
        .is_ok()
}

fn db_has_pending_capture_jobs(db_path: &Path) -> bool {
    let Ok(connection) = open_db(db_path) else {
        return false;
    };
    connection
        .query_row(
            "
            SELECT 1
            FROM capture_jobs
            WHERE status IN ('pending', 'failed')
              AND attempt_count < 3
            LIMIT 1
            ",
            [],
            |_| Ok(()),
        )
        .is_ok()
}

fn db_has_processing_capture_jobs(db_path: &Path) -> bool {
    let Ok(connection) = open_db(db_path) else {
        return false;
    };
    connection
        .query_row(
            "
            SELECT 1
            FROM capture_jobs
            WHERE status = 'processing'
            LIMIT 1
            ",
            [],
            |_| Ok(()),
        )
        .is_ok()
}

fn capture_pipeline_ready_for_next_capture(
    db_path: &Path,
    shared_state: &Arc<Mutex<SharedState>>,
) -> bool {
    let worker_busy = {
        let guard = shared_state.lock().unwrap();
        guard.capture_in_progress
            || (guard.worker_child.is_some()
                && !matches!(guard.worker_mode, Some(WorkerLaunchMode::Loop)))
    };
    if worker_busy {
        return false;
    }
    !db_has_pending_capture_jobs(db_path) && !db_has_processing_capture_jobs(db_path)
}

fn should_skip_capture(
    config: &DaemonConfig,
    sha256: &str,
    phash: &ImageHash,
    recent: &VecDeque<CaptureFingerprint>,
    db_path: &Path,
) -> Option<&'static str> {
    if db_has_capture_hash(db_path, sha256) {
        return Some("existing_db_hash");
    }
    recent_duplicate_reason(config, sha256, phash, recent)
}

fn recent_duplicate_reason(
    config: &DaemonConfig,
    sha256: &str,
    phash: &ImageHash,
    recent: &VecDeque<CaptureFingerprint>,
) -> Option<&'static str> {
    if recent
        .iter()
        .any(|fingerprint| fingerprint.sha256 == sha256)
    {
        return Some("recent_exact_duplicate_window");
    }
    if config.perceptual_dedupe_enabled
        && recent
            .iter()
            .any(|fingerprint| fingerprint.phash.dist(phash) <= config.phash_distance_threshold)
    {
        return Some("recent_visual_duplicate_window");
    }
    None
}

fn build_spool_path(spool_dir: &Path, sha256: &str) -> PathBuf {
    let timestamp: DateTime<Local> = SystemTime::now().into();
    spool_dir.join(format!(
        "{}-{}.png",
        timestamp.format("%Y%m%d-%H%M%S"),
        &sha256[..12]
    ))
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

fn rgba_image_from_raw(
    width: u32,
    height: u32,
    raw: Vec<u8>,
    color_format: DxgiDuplicationFormat,
) -> Result<RgbaImage> {
    let rgba8 = match color_format {
        DxgiDuplicationFormat::Bgra8 | DxgiDuplicationFormat::Bgra8Srgb => {
            let mut rgba = raw;
            for pixel in rgba.chunks_exact_mut(4) {
                pixel.swap(0, 2);
            }
            rgba
        }
        DxgiDuplicationFormat::Rgba8 | DxgiDuplicationFormat::Rgba8Srgb => raw,
        DxgiDuplicationFormat::Rgba16F => convert_rgba16f_to_rgba8(&raw),
        DxgiDuplicationFormat::Rgb10A2 | DxgiDuplicationFormat::Rgb10XrA2 => {
            convert_rgb10a2_to_rgba8(&raw)
        }
    };
    ImageBuffer::<Rgba<u8>, Vec<u8>>::from_raw(width, height, rgba8)
        .ok_or_else(|| anyhow!("invalid RGBA frame buffer for {width}x{height}"))
}

fn save_rgba_png(image: &RgbaImage, path: &Path) -> Result<()> {
    let file = File::create(path)
        .map_err(|error| anyhow!("failed to create PNG file {}: {error}", path.display()))?;
    let writer = BufWriter::new(file);
    let encoder = PngEncoder::new(writer);
    encoder
        .write_image(
            image.as_raw(),
            image.width(),
            image.height(),
            ExtendedColorType::Rgba8,
        )
        .map_err(|error| anyhow!("failed to encode PNG {}: {error}", path.display()))
}

fn convert_rgba16f_to_rgba8(raw: &[u8]) -> Vec<u8> {
    let mut converted = Vec::with_capacity(raw.len() / 2);
    for pixel in raw.chunks_exact(8) {
        let r = f16::from_bits(u16::from_le_bytes([pixel[0], pixel[1]])).to_f32();
        let g = f16::from_bits(u16::from_le_bytes([pixel[2], pixel[3]])).to_f32();
        let b = f16::from_bits(u16::from_le_bytes([pixel[4], pixel[5]])).to_f32();
        let a = f16::from_bits(u16::from_le_bytes([pixel[6], pixel[7]])).to_f32();
        converted.push(linear_to_srgb_u8(r));
        converted.push(linear_to_srgb_u8(g));
        converted.push(linear_to_srgb_u8(b));
        converted.push(float_to_u8(a));
    }
    converted
}

fn convert_rgb10a2_to_rgba8(raw: &[u8]) -> Vec<u8> {
    let mut converted = Vec::with_capacity(raw.len());
    for pixel in raw.chunks_exact(4) {
        let packed = u32::from_le_bytes([pixel[0], pixel[1], pixel[2], pixel[3]]);
        let r = (packed & 0x3ff) as f32 / 1023.0;
        let g = ((packed >> 10) & 0x3ff) as f32 / 1023.0;
        let b = ((packed >> 20) & 0x3ff) as f32 / 1023.0;
        let a = ((packed >> 30) & 0x03) as f32 / 3.0;
        converted.push(linear_to_srgb_u8(r));
        converted.push(linear_to_srgb_u8(g));
        converted.push(linear_to_srgb_u8(b));
        converted.push(float_to_u8(a));
    }
    converted
}

fn linear_to_srgb_u8(value: f32) -> u8 {
    let value = value.clamp(0.0, 1.0);
    let encoded = if value <= 0.003_130_8 {
        value * 12.92
    } else {
        (1.055 * value.powf(1.0 / 2.4)) - 0.055
    };
    float_to_u8(encoded)
}

fn float_to_u8(value: f32) -> u8 {
    if !value.is_finite() {
        return 0;
    }
    (value.clamp(0.0, 1.0) * 255.0).round() as u8
}

fn panic_payload_message(payload: Box<dyn std::any::Any + Send>) -> String {
    if let Some(message) = payload.downcast_ref::<&str>() {
        return (*message).to_string();
    }
    if let Some(message) = payload.downcast_ref::<String>() {
        return message.clone();
    }
    "unknown panic payload".to_string()
}

fn short_hash(value: &str) -> String {
    value.chars().take(12).collect()
}

fn short_path(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.display().to_string())
}

fn append_log_file(path: &Path, message: &str) {
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
        let timestamp = Local::now().format("%Y-%m-%d %H:%M:%S");
        let _ = writeln!(file, "[{}] {}", timestamp, message);
    }
}

fn main() -> Result<()> {
    let config = DaemonConfig::load();
    let tray_config = config.clone();
    let controller = Arc::new(Controller::new(config)?);
    let event_loop = EventLoop::<UserEvent>::with_user_event().build()?;
    let proxy = event_loop.create_proxy();
    spawn_manual_capture_hotkey_listener(&tray_config, proxy.clone());
    {
        let tick_proxy = proxy.clone();
        std::thread::spawn(move || {
            loop {
                std::thread::sleep(Duration::from_secs(1));
                let _ = tick_proxy.send_event(UserEvent::TooltipTick);
            }
        });
    }
    let tray_icon = build_tray_icon(proxy.clone(), &tray_config)?;
    let mut app = App {
        window: None,
        tray_icon,
        controller,
        proxy,
    };
    event_loop.run_app(&mut app)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_hash(fill: u8) -> ImageHash {
        let image = ImageBuffer::from_pixel(8, 8, Rgba([fill, fill, fill, 255]));
        HasherConfig::new().to_hasher().hash_image(&image)
    }

    #[test]
    fn recent_duplicate_reason_keeps_perceptual_matches_when_disabled() {
        let mut recent = VecDeque::new();
        recent.push_back(CaptureFingerprint {
            sha256: "first".to_string(),
            phash: sample_hash(32),
        });

        let mut config = DaemonConfig::default();
        config.perceptual_dedupe_enabled = false;
        config.phash_distance_threshold = 64;

        let reason = recent_duplicate_reason(&config, "second", &sample_hash(33), &recent);
        assert_eq!(reason, None);
    }

    #[test]
    fn recent_duplicate_reason_still_skips_exact_hash_matches() {
        let mut recent = VecDeque::new();
        recent.push_back(CaptureFingerprint {
            sha256: "same".to_string(),
            phash: sample_hash(32),
        });

        let config = DaemonConfig::default();
        let reason = recent_duplicate_reason(&config, "same", &sample_hash(255), &recent);
        assert_eq!(reason, Some("recent_exact_duplicate_window"));
    }

    #[test]
    fn recent_duplicate_reason_can_skip_perceptual_matches_when_enabled() {
        let mut recent = VecDeque::new();
        let phash = sample_hash(32);
        recent.push_back(CaptureFingerprint {
            sha256: "first".to_string(),
            phash: phash.clone(),
        });

        let mut config = DaemonConfig::default();
        config.perceptual_dedupe_enabled = true;
        config.phash_distance_threshold = 0;

        let reason = recent_duplicate_reason(&config, "second", &phash, &recent);
        assert_eq!(reason, Some("recent_visual_duplicate_window"));
    }

    #[test]
    fn daemon_config_from_toml_accepts_partial_overrides() {
        let config = DaemonConfig::from_toml(
            r#"
manual_capture_hotkey = "Alt+Q"
interval_seconds = 15
"#,
        )
        .expect("partial config should parse");

        assert_eq!(config.manual_capture_hotkey.as_deref(), Some("Alt+Q"));
        assert_eq!(config.interval_seconds, 15);
        assert_eq!(config.ocr_max_side, default_ocr_max_side());
        assert_eq!(config.dedupe_window, 12);
    }
}
