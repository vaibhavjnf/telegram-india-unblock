// worker.js — Telegram Bot API reverse proxy on Cloudflare Workers.
//
// WHY: When an ISP blocks api.telegram.org (e.g. India/Jio, Iran, Russia),
// your bot can't reach the Bot API directly. A Cloudflare Worker is served from
// Cloudflare's global anycast network — which ISPs cannot wholesale-block — and
// forwards your requests to Telegram from Cloudflare's edge (which is not
// blocked). Point your bot's API base_url at this Worker and it just works.
//
// FAST: served from the nearest Cloudflare PoP, typically sub-second. No proxy
// pool to rot, no rotation, no maintenance.
//
// USAGE: deploy this (see deploy.sh / README), then set your bot's base URL to
//   https://<worker-name>.<subdomain>.workers.dev/bot
// For Hermes: gateway.platforms.telegram.extra.base_url
//
// SECURITY NOTE: a bare reverse proxy will forward ANY /bot<token>/... request,
// so the Worker URL is only as secret as the bot token in the path (same trust
// model as api.telegram.org itself — the token IS the credential). Don't share
// the URL+token. Optionally add the SECRET_PREFIX gate below for defense in depth.

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // Friendly homepage so hitting the root in a browser isn't a scary 404.
    if (url.pathname === "/" && !url.search) {
      return new Response(
        "Telegram Bot API reverse proxy is running.\n" +
          "Point your bot's API base_url at https://" + url.host + "/bot\n",
        { headers: { "content-type": "text/plain; charset=utf-8" } }
      );
    }

    // --- Optional secret-path gate (uncomment + set to require a prefix) ---
    // const SECRET_PREFIX = "/s/CHANGE_ME";
    // if (!url.pathname.startsWith(SECRET_PREFIX)) return new Response("Not found", { status: 404 });
    // url.pathname = url.pathname.slice(SECRET_PREFIX.length);

    // Forward everything to Telegram, preserving method, path, query and body.
    url.hostname = "api.telegram.org";
    url.protocol = "https:";
    url.port = "";

    // Strip Cloudflare/edge identity headers so we don't leak the requester.
    const headers = new Headers(request.headers);
    [
      "host", "cf-connecting-ip", "cf-ray", "cf-ipcountry", "cf-visitor",
      "x-forwarded-for", "x-forwarded-proto", "x-real-ip", "connection",
    ].forEach((h) => headers.delete(h));
    // Telegram is fine without a UA, but some clients/WAFs prefer one present.
    if (!headers.has("user-agent")) {
      headers.set("user-agent", "tg-bot-api-proxy/1.0");
    }

    const init = {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
      redirect: "follow",
    };

    const resp = await fetch(url.toString(), init);

    // Pass the response straight back, minus cookies (Telegram sets none, but
    // belt-and-braces for any intermediary).
    const out = new Headers(resp.headers);
    out.delete("set-cookie");
    out.delete("set-cookie2");
    return new Response(resp.body, {
      status: resp.status,
      statusText: resp.statusText,
      headers: out,
    });
  },
};
