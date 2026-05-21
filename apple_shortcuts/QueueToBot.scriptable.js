// Queue Bot uploader for Scriptable.
//
// Setup:
// 1. Replace BASE_URL with your cloud app URL, without trailing slash.
// 2. Replace QUEUE_API_TOKEN with the same value you set on the bot host.
// 3. In Scriptable, enable this script in the Share Sheet for Images and File URLs.
// 4. For large videos, enable "Run in App" in the script or Shortcut settings.

const BASE_URL = "https://YOUR-APP-DOMAIN";
const QUEUE_API_TOKEN = "PASTE_QUEUE_API_TOKEN_HERE";
const ASK_FOR_CAPTION = false;

const PHOTO_EXTENSIONS = new Set(["jpg", "jpeg", "png", "heic", "heif", "webp"]);
const VIDEO_EXTENSIONS = new Set(["mp4", "mov", "m4v", "webm"]);

function cleanBaseUrl(url) {
  return String(url || "").replace(/\/+$/, "");
}

function fileUrlToPath(fileUrl) {
  if (!fileUrl) return "";
  let value = String(fileUrl);
  if (value.startsWith("file://")) value = value.slice("file://".length);
  return decodeURIComponent(value);
}

function filenameFromPath(path) {
  const parts = String(path).split("/");
  return parts[parts.length - 1] || "upload";
}

function extensionFromName(name) {
  const match = String(name).toLowerCase().match(/\.([a-z0-9]+)$/);
  return match ? match[1] : "";
}

function mediaTypeFromFilename(name) {
  const ext = extensionFromName(name);
  if (PHOTO_EXTENSIONS.has(ext)) return "photo";
  if (VIDEO_EXTENSIONS.has(ext)) return "video";
  return "";
}

async function chooseMediaType(filename) {
  const alert = new Alert();
  alert.title = "Tipo media";
  alert.message = `Non riconosco il tipo di "${filename}".`;
  alert.addAction("Foto");
  alert.addAction("Video");
  alert.addCancelAction("Annulla");
  const choice = await alert.present();
  if (choice === 0) return "photo";
  if (choice === 1) return "video";
  throw new Error("Operazione annullata.");
}

async function askCaption() {
  if (!ASK_FOR_CAPTION) return "";

  const alert = new Alert();
  alert.title = "Caption";
  alert.message = "Lascia vuoto se non vuoi aggiungere testo.";
  alert.addTextField("Caption", "");
  alert.addAction("Continua");
  alert.addCancelAction("Senza caption");
  const choice = await alert.present();
  if (choice === -1) return "";
  return alert.textFieldValue(0).trim();
}

async function uploadFile(path, caption) {
  const filename = filenameFromPath(path);
  let mediaType = mediaTypeFromFilename(filename);
  if (!mediaType) mediaType = await chooseMediaType(filename);

  const request = new Request(`${cleanBaseUrl(BASE_URL)}/api/queue/${mediaType}`);
  request.method = "POST";
  request.timeoutInterval = 180;
  request.headers = { Authorization: `Bearer ${QUEUE_API_TOKEN}` };
  if (caption) request.addParameterToMultipart("caption", caption);
  request.addFileToMultipart(path, "media", filename);

  const json = await request.loadJSON();
  if (request.response.statusCode < 200 || request.response.statusCode >= 300 || !json.ok) {
    throw new Error(`${filename}: ${JSON.stringify(json)}`);
  }
  return json;
}

async function uploadImage(image, index, caption) {
  const request = new Request(`${cleanBaseUrl(BASE_URL)}/api/queue/photo`);
  request.method = "POST";
  request.timeoutInterval = 180;
  request.headers = { Authorization: `Bearer ${QUEUE_API_TOKEN}` };
  if (caption) request.addParameterToMultipart("caption", caption);
  request.addImageToMultipart(image, "media", `shared-image-${index + 1}.jpg`);

  const json = await request.loadJSON();
  if (request.response.statusCode < 200 || request.response.statusCode >= 300 || !json.ok) {
    throw new Error(`image ${index + 1}: ${JSON.stringify(json)}`);
  }
  return json;
}

function assertConfigured() {
  if (!BASE_URL || BASE_URL.includes("YOUR-APP-DOMAIN")) {
    throw new Error("Configura BASE_URL nello script.");
  }
  if (!QUEUE_API_TOKEN || QUEUE_API_TOKEN.includes("PASTE_QUEUE_API_TOKEN_HERE")) {
    throw new Error("Configura QUEUE_API_TOKEN nello script.");
  }
}

async function notify(title, body) {
  const notification = new Notification();
  notification.title = title;
  notification.body = body;
  await notification.schedule();
}

async function main() {
  assertConfigured();

  const caption = await askCaption();
  const results = [];
  const fileUrls = Array.isArray(args.fileURLs) ? args.fileURLs : [];
  const images = Array.isArray(args.images) ? args.images : [];

  for (const fileUrl of fileUrls) {
    const path = fileUrlToPath(fileUrl);
    if (path) results.push(await uploadFile(path, caption));
  }

  for (let i = 0; i < images.length; i += 1) {
    results.push(await uploadImage(images[i], i, caption));
  }

  if (results.length === 0) {
    throw new Error("Nessun file ricevuto. Apri lo script dal foglio di condivisione di Foto/File.");
  }

  const duplicates = results.filter((item) => item.queue_status === "duplicate").length;
  const added = results.length - duplicates;
  await notify("Queue Bot", `Aggiunti: ${added}. Duplicati: ${duplicates}.`);
}

try {
  await main();
} catch (error) {
  await notify("Queue Bot - errore", String(error.message || error));
  throw error;
}
