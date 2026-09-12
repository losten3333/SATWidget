// SATWidget GP Updater — background service worker.
//
// Автономно по расписанию (chrome.alarms) скачивает GP-данные and отправляет
// их в Python-приложение SATWidget на локальный HTTP-приёмник.
//
// Space-Track использует cookies текущей (авторизованной) сессии Chrome —
// пароли нигде не хранятся. Если Space-Track недоступен или не авторизован,
// используется публичный CelesTrak.

const DEFAULT_CONFIG = {
  port: 9090,
  intervalMinutes: 120,
  provider: "space_track", // "space_track" | "celestrak" | "both"
  spaceTrackQuery:
    "basicspacedata/query/class/gp/EPOCH/%3Enow-30/ORDERBY/NORAD_CAT_ID/format/json",
};

const SPACE_TRACK_BASE = "https://www.space-track.org";
const CELESTRAK_URL =
  "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=json";

async function getConfig() {
  const stored = await chrome.storage.sync.get(DEFAULT_CONFIG);
  return { ...DEFAULT_CONFIG, ...stored };
}

function log(...args) {
  console.log("[SATWidget GP Updater]", ...args);
}

function isGpList(data) {
  return (
    Array.isArray(data) &&
    data.length > 0 &&
    typeof data[0] === "object" &&
    data[0] !== null
  );
}

// Получаем GP-данные с Space-Track, используя активную сессию браузера.
async function fetchSpaceTrack(query) {
  const url = `${SPACE_TRACK_BASE}/${query}`;
  const response = await fetch(url, {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`Space-Track HTTP ${response.status}: ${text.slice(0, 200)}`);
  }
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    throw new Error(`Space-Track не JSON: ${text.slice(0, 200)}`);
  }
  if (!isGpList(data)) {
    throw new Error("Space-Track вернул некорректный GP");
  }
  return data;
}

// Получаем GP-данные с публичного CelesTrak.
async function fetchCelestrak() {
  const response = await fetch(CELESTRAK_URL, {
    headers: { Accept: "application/json" },
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`CelesTrak HTTP ${response.status}: ${text.slice(0, 200)}`);
  }
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    throw new Error(`CelesTrak не JSON: ${text.slice(0, 200)}`);
  }
  if (!isGpList(data)) {
    throw new Error("CelesTrak вернул некорректный GP");
  }
  return data;
}

async function downloadGp(config) {
  const attempts = [];
  if (config.provider === "space_track" || config.provider === "both") {
    try {
      const data = await fetchSpaceTrack(config.spaceTrackQuery);
      log(`Space-Track: ${data.length} записей`);
      return data;
    } catch (e) {
      log("Space-Track не удался:", e.message);
      attempts.push(`space_track: ${e.message}`);
    }
  }
  if (config.provider === "celestrak" || config.provider === "both") {
    try {
      const data = await fetchCelestrak();
      log(`CelesTrak: ${data.length} записей`);
      return data;
    } catch (e) {
      log("CelesTrak не удался:", e.message);
      attempts.push(`celestrak: ${e.message}`);
    }
  }
  throw new Error(`Все источники недоступны: ${attempts.join("; ")}`);
}

async function postToReceiver(data, port) {
  const url = `http://127.0.0.1:${port}/gp`;
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    throw new Error(`Приёмник HTTP ${response.status}: ${await response.text()}`);
  }
  return response;
}

async function runUpdate() {
  const config = await getConfig();
  log("Запускаю обновление GP...");
  try {
    const data = await downloadGp(config);
    await postToReceiver(data, config.port);
    log(`Отправлено ${data.length} записей на порт ${config.port}`);
  } catch (e) {
    log("Ошибка обновления:", e.message);
  }
}

// Однократная установка расписания.
async function setupAlarmIfNeeded() {
  const config = await getConfig();
  const existing = await chrome.alarms.get("gp_update");
  const wanted = Math.max(1, config.intervalMinutes);
  if (!existing || existing.periodInMinutes !== wanted) {
    await chrome.alarms.create("gp_update", { periodInMinutes: wanted });
    log(`Расписание установлено: каждые ${wanted} мин`);
  }
}

chrome.runtime.onInstalled.addListener(() => {
  setupAlarmIfNeeded();
  runUpdate();
});

chrome.runtime.onStartup.addListener(() => {
  setupAlarmIfNeeded();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "gp_update") {
    runUpdate();
  }
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "sync" && (changes.intervalMinutes || changes.port || changes.provider)) {
    setupAlarmIfNeeded();
  }
});

// Запускаем при первой загрузке service worker (для пустых установок/перезапусков).
setupAlarmIfNeeded();
