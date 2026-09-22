// SATWidget GP Updater — background service worker.
//
// Расширение НЕ скачивает данные самостоятельно: оно опрашивает HTTP-приёмник
// виджета (GET /status на 127.0.0.1:<port>) и скачивает GP-данные только тогда,
// когда виджет сообщает needs_update=true (файл gp.json старше tle.update_hours).
// Скачанные данные отправляются в виджет (POST /gp), который записывает их в
// gp.json.
//
// Если сессия Space-Track потеряна, расширение выполняет вход напрямую
// (form POST /auth/login с CSRF-токеном), используя учётные данные, которые
// виджет передаёт в GET /status (секция space_track в config.json). Расширение
// НЕ хранит логин/пароль — они существуют только в памяти на время обновления.

const DEFAULT_CONFIG = {
  port: 9090,
  pollMinutes: 1,
  provider: "space_track", // "space_track" | "celestrak" | "both"
  spaceTrackQuery:
    "basicspacedata/query/class/gp/EPOCH/%3Enow-30/ORDERBY/NORAD_CAT_ID/format/json",
};

const SPACE_TRACK_BASE = "https://www.space-track.org";
const SPACE_TRACK_LOGIN_URL = "https://www.space-track.org/auth/login";
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
  // Сессия Space-Track могла истечь: вместо JSON приходит страница логина
  // (HTML) или пустое тело. Это тоже означает «нужна авторизация».
  const trimmed = text.trimStart();
  if (!trimmed || trimmed.startsWith("<")) {
    throw new Error("Space-Track требует авторизации (получен HTML)");
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

function isAuthError(message) {
  return /HTTP 40[13]|требует авторизации|требует входа|Space-Track не JSON/i.test(message);
}

// Получаем CSRF-токен со страницы входа Space-Track.
async function fetchCsrfToken() {
  const response = await fetch(SPACE_TRACK_LOGIN_URL, {
    credentials: "include",
  });
  const html = await response.text();
  const match =
    html.match(
      /name=["']spacetrack_csrf_token["'][^>]*value=["']([^"']*)["']/i
    ) ||
    html.match(
      /value=["']([^"']*)["'][^>]*name=["']spacetrack_csrf_token["']/i
    );
  return match ? match[1] : "";
}

// Вход на Space-Track напрямую: form POST /auth/login с CSRF-токеном.
// Учётные данные берутся только из аргумента (переданы виджетом в /status)
// и нигде не сохраняются.
async function loginSpaceTrack(user, password) {
  const csrf = await fetchCsrfToken();
  if (!csrf) {
    log("Space-Track: не удалось получить CSRF-токен");
    return false;
  }
  const body = new URLSearchParams({
    identity: user,
    password: password,
    spacetrack_csrf_token: csrf,
  });
  const response = await fetch(SPACE_TRACK_LOGIN_URL, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
    redirect: "follow",
  });
  log(`Space-Track: вход, HTTP ${response.status}`);
  return response.ok;
}

async function downloadGp(config, credentials) {
  const attempts = [];
  if (config.provider === "space_track" || config.provider === "both") {
    try {
      const data = await fetchSpaceTrack(config.spaceTrackQuery);
      log(`Space-Track: ${data.length} записей`);
      return data;
    } catch (e) {
      log("Space-Track не удался:", e.message);
      attempts.push(`space_track: ${e.message}`);
      if (isAuthError(e.message) && credentials && credentials.user) {
        const loggedIn = await loginSpaceTrack(
          credentials.user,
          credentials.password
        );
        if (loggedIn) {
          try {
            const data = await fetchSpaceTrack(config.spaceTrackQuery);
            log(`Space-Track (после входа): ${data.length} записей`);
            return data;
          } catch (e2) {
            log("Space-Track после входа всё ещё недоступен:", e2.message);
            attempts.push(`space_track_after_login: ${e2.message}`);
          }
        } else {
          log("Space-Track: вход не выполнен — проверьте space_track в config.json");
        }
      }
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

// Опрашивает виджет: нужны ли свежие GP-данные (по tle.update_hours).
// Возвращает { needsUpdate, credentials }. Учётные данные Space-Track
// присутствуют только если needs_update=true (виджет передал их в /status).
async function checkWidgetDemand(config) {
  try {
    const response = await fetch(`http://127.0.0.1:${config.port}/status`, {
      cache: "no-store",
    });
    if (!response.ok) {
      log(`Виджет недоступен (HTTP ${response.status})`);
      return { needsUpdate: false, credentials: null };
    }
    const status = await response.json();
    log(
      `Виджет: needs_update=${status.needs_update}, ` +
        `cache=${status.cache}, exists=${status.cache_exists}`
    );
    return {
      needsUpdate: status.needs_update === true,
      credentials:
        status.needs_update && status.space_track_user
          ? {
              user: status.space_track_user,
              password: status.space_track_password || "",
            }
          : null,
    };
  } catch (e) {
    log("Виджет недоступен:", e.message);
    return { needsUpdate: false, credentials: null };
  }
}

async function runUpdate(config, credentials) {
  log("Виджет запросил обновление — скачиваю GP...");
  try {
    const data = await downloadGp(config, credentials);
    await postToReceiver(data, config.port);
    log(`Отправлено ${data.length} записей на порт ${config.port}`);
  } catch (e) {
    log("Ошибка обновления:", e.message);
  }
}

// Один цикл опроса: спросить виджет и, при необходимости, обновить данные.
async function poll() {
  const config = await getConfig();
  const demand = await checkWidgetDemand(config);
  if (demand.needsUpdate) {
    await runUpdate(config, demand.credentials);
  }
}

// Однократная установка расписания опроса виджета.
async function setupAlarmIfNeeded() {
  const config = await getConfig();
  const existing = await chrome.alarms.get("gp_poll");
  const wanted = Math.max(1, config.pollMinutes);
  if (!existing || existing.periodInMinutes !== wanted) {
    await chrome.alarms.create("gp_poll", { periodInMinutes: wanted });
    log(`Опрос виджета: каждые ${wanted} мин`);
  }
}

chrome.runtime.onInstalled.addListener(() => {
  setupAlarmIfNeeded();
  poll();
});

chrome.runtime.onStartup.addListener(() => {
  setupAlarmIfNeeded();
  poll();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "gp_poll") {
    poll();
  }
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (
    area === "sync" &&
    (changes.port || changes.pollMinutes || changes.provider)
  ) {
    setupAlarmIfNeeded();
  }
});

// Запускаем при первой загрузке service worker (для пустых установок/перезапусков).
setupAlarmIfNeeded();