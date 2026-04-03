use std::collections::VecDeque;
use std::fs::{self, File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::atomic::{AtomicBool, Ordering};
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
use winit::application::ApplicationHandler;
use winit::event::WindowEvent;
use winit::event_loop::{ActiveEventLoop, EventLoop, EventLoopProxy};
use winit::window::Window;

#[derive(Clone, Debug, Eq, PartialEq)]
enum UserEvent {
    Start,
    Pause,
    Resume,
    ProcessNextJob,
    RestartApp,
    TooltipTick,
    OpenLogs,
    TailLogs,
    ToggleDebug,
    Quit,
    WorkerLoopExited,
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
}

#[derive(Clone, Debug, Deserialize)]
struct DaemonConfig {
    interval_seconds: u64,
    dedupe_window: usize,
    phash_distance_threshold: u32,
    #[serde(default)]
    auto_start_worker: bool,
    db_path: PathBuf,
    spool_dir: PathBuf,
    log_path: PathBuf,
    python_executable: String,
    python_args: Vec<String>,
    worker_script: PathBuf,
}

impl Default for DaemonConfig {
    fn default() -> Self {
        let jp_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("JP parent directory")
            .to_path_buf();
        Self {
            interval_seconds: 60,
            dedupe_window: 12,
            phash_distance_threshold: 6,
            auto_start_worker: false,
            db_path: jp_root.join("data").join("lang_jp.sqlite3"),
            spool_dir: jp_root.join("data").join("capture_spool"),
            log_path: jp_root.join("data").join("capture_daemon.log"),
            python_executable: "py".to_string(),
            python_args: vec!["-3.12".to_string()],
            worker_script: jp_root.join("capture_worker.py"),
        }
    }
}

impl DaemonConfig {
    fn load() -> Self {
        let config_path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("capture_daemon.toml");
        let default = Self::default();
        let Ok(raw) = fs::read_to_string(&config_path) else {
            return default;
        };
        toml::from_str(&raw).unwrap_or(default)
    }
}

struct SharedState {
    state: ServiceState,
    worker_child: Option<Child>,
    worker_mode: Option<WorkerLaunchMode>,
    debug_enabled: bool,
    capture_in_progress: bool,
    next_capture_at: Option<SystemTime>,
}

struct Controller {
    runtime: Runtime,
    config: DaemonConfig,
    state: Arc<Mutex<SharedState>>,
    stop_flag: Arc<AtomicBool>,
    pause_flag: Arc<AtomicBool>,
    debug_flag: Arc<AtomicBool>,
}

impl Controller {
    fn new(config: DaemonConfig) -> Result<Self> {
        Ok(Self {
            runtime: Runtime::new().context("failed to create tokio runtime")?,
            config,
            state: Arc::new(Mutex::new(SharedState {
                state: ServiceState::Idle,
                worker_child: None,
                worker_mode: None,
                debug_enabled: false,
                capture_in_progress: false,
                next_capture_at: None,
            })),
            stop_flag: Arc::new(AtomicBool::new(false)),
            pause_flag: Arc::new(AtomicBool::new(false)),
            debug_flag: Arc::new(AtomicBool::new(false)),
        })
    }

    fn state(&self) -> ServiceState {
        self.state.lock().unwrap().state
    }

    fn set_state(&self, state: ServiceState) {
        self.state.lock().unwrap().state = state;
    }

    fn set_capture_schedule(&self, in_progress: bool, next_capture_at: Option<SystemTime>) {
        let mut guard = self.state.lock().unwrap();
        guard.capture_in_progress = in_progress;
        guard.next_capture_at = next_capture_at;
    }

    fn tooltip_details(&self) -> String {
        let guard = self.state.lock().unwrap();
        if guard.state != ServiceState::Running {
            return match guard.state {
                ServiceState::Idle => "Idle".to_string(),
                ServiceState::Paused => "Paused".to_string(),
                ServiceState::WorkerError => "Worker error".to_string(),
                ServiceState::Running => "Running".to_string(),
            };
        }
        if guard.capture_in_progress {
            return "Running | capturing now".to_string();
        }
        if let Some(deadline) = guard.next_capture_at {
            let remaining = deadline
                .duration_since(SystemTime::now())
                .unwrap_or(Duration::ZERO)
                .as_secs();
            return format!("Running | next capture in {remaining}s");
        }
        "Running".to_string()
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
            "capture service started interval={}s dedupe_window={} phash_threshold={} auto_start_worker={} debug={}",
            self.config.interval_seconds,
            self.config.dedupe_window,
            self.config.phash_distance_threshold,
            self.config.auto_start_worker,
            self.debug_enabled()
        ));
        if self.config.auto_start_worker {
            self.spawn_worker(WorkerLaunchMode::Loop, Some(proxy));
        } else {
            self.append_log("automatic OCR worker disabled; capture jobs will stay pending until processed manually");
        }
        self.spawn_capture_loop();
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
        }
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
        let escaped = self
            .config
            .log_path
            .to_string_lossy()
            .replace('\'', "''");
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
        command.arg(match mode {
            WorkerLaunchMode::Loop => "loop",
            WorkerLaunchMode::Once => "once",
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
                std::thread::spawn(move || loop {
                    std::thread::sleep(Duration::from_secs(1));
                    let mut guard = state.lock().unwrap();
                    let Some(child) = guard.worker_child.as_mut() else {
                        break;
                    };
                    match child.try_wait() {
                        Ok(Some(status)) => {
                            let exited_mode = guard.worker_mode.take();
                            let timestamp = Local::now().format("%Y-%m-%d %H:%M:%S");
                            if let Ok(mut file) = OpenOptions::new()
                                .create(true)
                                .append(true)
                                .open(&log_path)
                            {
                                let _ = writeln!(
                                    file,
                                    "[{}] capture worker exited mode={:?} status={}",
                                    timestamp, exited_mode, status
                                );
                            }
                            guard.worker_child = None;
                            if matches!(exited_mode, Some(WorkerLaunchMode::Loop)) {
                                if let Some(proxy) = proxy.clone() {
                                    let _ = proxy.send_event(UserEvent::WorkerLoopExited);
                                }
                            }
                            break;
                        }
                        Ok(None) => {}
                        Err(_) => {
                            let exited_mode = guard.worker_mode.take();
                            guard.worker_child = None;
                            if matches!(exited_mode, Some(WorkerLaunchMode::Loop)) {
                                if let Some(proxy) = proxy.clone() {
                                    let _ = proxy.send_event(UserEvent::WorkerLoopExited);
                                }
                            }
                            break;
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

    fn restart_app(&self) -> Result<()> {
        let current_exe = std::env::current_exe().context("failed to locate current executable")?;
        Command::new(&current_exe)
            .spawn()
            .with_context(|| format!("failed to relaunch {}", current_exe.display()))?;
        self.append_log(&format!("app restart requested via {}", current_exe.display()));
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

    fn spawn_capture_loop(&self) {
        let config = self.config.clone();
        let stop_flag = Arc::clone(&self.stop_flag);
        let pause_flag = Arc::clone(&self.pause_flag);
        let debug_flag = Arc::clone(&self.debug_flag);
        let shared_state = Arc::clone(&self.state);
        let log_path = self.config.log_path.clone();
        self.runtime.spawn_blocking(move || {
            append_log_file(
                &log_path,
                &format!(
                    "capture loop initialized interval={}s dedupe_window={} phash_threshold={} debug={}",
                    config.interval_seconds,
                    config.dedupe_window,
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
                                &format!("skipped capture sha={} reason={reason}", sha256),
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
                                &format!("saved spool image to {}", temp_path.display()),
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
                                        "queued capture job #{job_id} sha={} temp_image={}",
                                        sha256,
                                        temp_path.display()
                                    ),
                                );
                                recent.push_back(CaptureFingerprint { sha256, phash });
                                while recent.len() > config.dedupe_window {
                                    recent.pop_front();
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

                let next_capture_at = SystemTime::now()
                    .checked_add(Duration::from_secs(config.interval_seconds));
                {
                    let mut guard = shared_state.lock().unwrap();
                    guard.capture_in_progress = false;
                    guard.next_capture_at = next_capture_at;
                }
                append_log_file(
                    &log_path,
                    &format!("next capture scheduled in {}s", config.interval_seconds),
                );

                std::thread::sleep(Duration::from_secs(config.interval_seconds));
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
            UserEvent::ProcessNextJob => self.controller.process_next_job(),
            UserEvent::RestartApp => {
                match self.controller.restart_app() {
                    Ok(()) => {
                        self.controller.stop();
                        event_loop.exit();
                    }
                    Err(error) => self
                        .controller
                        .append_log(&format!("failed to restart app: {error}")),
                }
            }
            UserEvent::TooltipTick => {}
            UserEvent::OpenLogs => self.controller.open_logs(),
            UserEvent::TailLogs => self.controller.tail_logs(),
            UserEvent::ToggleDebug => self.controller.toggle_debug(),
            UserEvent::Quit => {
                self.controller.stop();
                event_loop.exit();
            }
            UserEvent::WorkerLoopExited => self.controller.set_state(ServiceState::WorkerError),
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

fn build_tray_icon(proxy: EventLoopProxy<UserEvent>) -> Result<TrayIcon<UserEvent>> {
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
                .item("Process next OCR job", UserEvent::ProcessNextJob)
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
            temp_path.to_string_lossy().as_ref(),
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
    if recent.iter().any(|fingerprint| {
        fingerprint.sha256 == sha256
            || fingerprint.phash.dist(phash) <= config.phash_distance_threshold
    }) {
        return Some("recent_duplicate_window");
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
    let controller = Arc::new(Controller::new(config)?);
    let event_loop = EventLoop::<UserEvent>::with_user_event().build()?;
    let proxy = event_loop.create_proxy();
    {
        let tick_proxy = proxy.clone();
        std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_secs(1));
            let _ = tick_proxy.send_event(UserEvent::TooltipTick);
        });
    }
    let tray_icon = build_tray_icon(proxy.clone())?;
    let mut app = App {
        window: None,
        tray_icon,
        controller,
        proxy,
    };
    event_loop.run_app(&mut app)?;
    Ok(())
}
