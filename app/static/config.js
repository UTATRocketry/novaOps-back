const isHttps = window.location.protocol === "https:";
window.APP_CONFIG = {
  BASE_URL: window.location.origin,
  WS_URL: `${isHttps ? "wss" : "ws"}://${window.location.host}`
};
