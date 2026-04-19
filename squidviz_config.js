// SquidViz Python backend URL as seen by the browser.
//
// Recommended production layout:
//   Keep this blank and reverse-proxy /json/ on the web server to the
//   Python service. Browser clients then only need access to the web server.
//
// Direct browser-to-service testing example:
//   window.SQUIDVIZ_BACKEND_URL = "http://cephmachine:8081";
window.SQUIDVIZ_BACKEND_URL = "";
