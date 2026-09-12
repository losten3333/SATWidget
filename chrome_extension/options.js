const DEFAULTS = {
  port: 9090,
  intervalMinutes: 120,
  provider: "space_track",
};

async function load() {
  const config = await chrome.storage.sync.get(DEFAULTS);
  document.getElementById("port").value = config.port;
  document.getElementById("interval").value = config.intervalMinutes;
  document.getElementById("provider").value = config.provider;
}

async function save() {
  const port = parseInt(document.getElementById("port").value, 10);
  const interval = parseInt(document.getElementById("interval").value, 10);
  const provider = document.getElementById("provider").value;

  await chrome.storage.sync.set({
    port: Number.isFinite(port) ? port : DEFAULTS.port,
    intervalMinutes: Number.isFinite(interval) ? interval : DEFAULTS.intervalMinutes,
    provider,
  });

  const status = document.getElementById("status");
  status.textContent = "Сохранено ✓";
  setTimeout(() => { status.textContent = ""; }, 2000);
}

document.getElementById("save").addEventListener("click", save);
document.addEventListener("DOMContentLoaded", load);
