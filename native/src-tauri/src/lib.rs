#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;
use tauri::{Manager, RunEvent, State};

struct BackendProcess(Mutex<Option<Child>>);
struct BackendPort(Mutex<u16>);

fn project_root() -> PathBuf {
    let mut starts = Vec::new();
    if let Ok(current) = std::env::current_dir() {
        starts.push(current);
    }
    if let Ok(executable) = std::env::current_exe() {
        if let Some(parent) = executable.parent() {
            starts.push(parent.to_path_buf());
        }
    }
    for start in starts {
        for candidate in start.ancestors() {
            if candidate.join("app.py").is_file() {
                return candidate.to_path_buf();
            }
        }
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

fn backend_is_running(port: u16) -> bool {
    std::net::TcpStream::connect_timeout(
        &format!("127.0.0.1:{port}").parse().expect("valid local address"),
        Duration::from_millis(250),
    )
    .is_ok()
}

fn terminate_child(child: &mut Child) {
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .output();
    }
    #[cfg(not(windows))]
    {
        let _ = child.kill();
    }
}

fn start_backend(app: &tauri::AppHandle) -> Result<(Option<Child>, u16), String> {
    let data_dir = app
        .path()
        .app_data_dir()
        .unwrap_or_else(|_| project_root().join("data"));
    let _ = std::fs::create_dir_all(&data_dir);
    let port_file = data_dir.join("backend.port");
    let _ = std::fs::remove_file(&port_file);
    let bundled_backend = app
        .path()
        .resource_dir()
        .ok()
        .map(|dir| dir.join("backend").join("research-companion-backend.exe"));
    let mut command = if let Some(path) = bundled_backend.filter(|path| path.is_file()) {
        let mut command = Command::new(path);
        command.args(["--port", "0"]);
        command
    } else {
        let root = project_root();
        let mut command = Command::new("python");
        command.current_dir(root).args(["app.py", "--port", "0"]);
        command
    };
    command.env("RESEARCH_COMPANION_DATA_DIR", data_dir);
    command.env("RESEARCH_COMPANION_PORT_FILE", &port_file);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let mut child = command.spawn().map_err(|error| format!("failed to start backend: {error}"))?;
    let mut port = 0;
    for _ in 0..40 {
        if let Ok(value) = std::fs::read_to_string(&port_file) {
            if let Ok(value) = value.trim().parse::<u16>() {
                if value > 0 && backend_is_running(value) {
                    port = value;
                    break;
                }
            }
        }
        thread::sleep(Duration::from_millis(150));
    }
    if port == 0 {
        terminate_child(&mut child);
        return Err("backend did not publish a listening port".to_string());
    }
    Ok((Some(child), port))
}

#[tauri::command]
fn get_backend_port(state: State<'_, BackendPort>) -> Result<u16, String> {
    let port = *state.0.lock().map_err(|_| "backend port lock poisoned")?;
    if port == 0 { Err("backend is not ready".to_string()) } else { Ok(port) }
}

#[tauri::command]
fn pick_directory() -> Option<String> {
    rfd::FileDialog::new()
        .set_title("Choose Agent working directory")
        .pick_folder()
        .map(|path| path.to_string_lossy().into_owned())
}

#[tauri::command]
fn pick_pdf() -> Option<String> {
    rfd::FileDialog::new()
        .set_title("Choose a research PDF")
        .add_filter("PDF documents", &["pdf"])
        .pick_file()
        .map(|path| path.to_string_lossy().into_owned())
}

fn kill_backend(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<BackendProcess>() {
        if let Ok(mut process) = state.0.lock() {
            if let Some(child) = process.as_mut() {
                terminate_child(child);
            }
            *process = None;
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let (backend, port) = start_backend(app.handle())?;
            app.manage(BackendProcess(Mutex::new(backend)));
            app.manage(BackendPort(Mutex::new(port)));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_backend_port, pick_directory, pick_pdf])
        .build(tauri::generate_context!())
        .expect("error while building Tauri application")
        .run(|app, event| {
            if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
                kill_backend(app);
            }
        });
}
