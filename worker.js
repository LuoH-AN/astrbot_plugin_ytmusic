export default {
  async fetch(request, env, ctx) {
    // incoming host -> upstream host (or full URL)
    const DOMAIN_MAP = {
      // Telegram Bot API
      "api.telegram.bot.luoh.org": "api.telegram.org",

      // Discord API / Gateway / CDN
      "api.discord.bot.luoh.org": "discord.com",
      "gateway.discord.bot.luoh.org": "gateway.discord.gg",
      "cdn.discord.bot.luoh.org": "cdn.discordapp.com",
      "media.discord.bot.luoh.org": "media.discordapp.net",

      // YouTube Music (for ytmusicapi)
      "music.youtube.bot.luoh.org": "music.youtube.com",

    };

    const incomingUrl = new URL(request.url);
    const incomingHost = incomingUrl.hostname;
    const target = resolveTarget(DOMAIN_MAP[incomingHost]);

    if (!target) {
      return json(
        {
          error: "No proxy rule matched",
          host: incomingHost,
          available: Object.keys(DOMAIN_MAP),
        },
        403,
        buildCorsHeaders(request),
      );
    }

    // CORS preflight can return directly.
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: buildCorsHeaders(request),
      });
    }

    const upstreamUrl = buildUpstreamUrl(incomingUrl, target);

    try {
      if (isWebSocketUpgrade(request)) {
        const wsRequest = new Request(upstreamUrl.toString(), {
          method: "GET",
          headers: buildProxyHeaders(request.headers, {
            websocket: true,
            incomingHost,
            incomingProto: incomingUrl.protocol,
          }),
        });
        return await fetch(wsRequest);
      }

      const init = {
        method: request.method,
        headers: buildProxyHeaders(request.headers, {
          websocket: false,
          incomingHost,
          incomingProto: incomingUrl.protocol,
        }),
        redirect: "manual",
      };

      if (!["GET", "HEAD"].includes(request.method)) {
        init.body = request.body;
      }

      const upstreamRequest = new Request(upstreamUrl.toString(), init);
      const upstreamResponse = await fetch(upstreamRequest);

      const responseHeaders = new Headers(upstreamResponse.headers);
      rewriteLocationHeader(responseHeaders, {
        incomingUrl,
        upstreamUrl,
        targetHost: target.host,
      });
      appendCorsHeaders(responseHeaders, request);

      return new Response(upstreamResponse.body, {
        status: upstreamResponse.status,
        statusText: upstreamResponse.statusText,
        headers: responseHeaders,
      });
    } catch (err) {
      return json(
        {
          error: "Upstream fetch failed",
          host: incomingHost,
          target: target.host,
          detail: err instanceof Error ? err.message : String(err),
        },
        502,
        buildCorsHeaders(request),
      );
    }
  },
};

const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "proxy-connection",
  "keep-alive",
  "transfer-encoding",
  "te",
  "trailer",
  "upgrade",
]);

function resolveTarget(raw) {
  if (!raw) {
    return null;
  }

  let parsed;
  try {
    parsed = raw.includes("://") ? new URL(raw) : new URL(`https://${raw}`);
  } catch {
    return null;
  }

  let protocol = parsed.protocol;
  if (protocol === "ws:" || protocol === "wss:") {
    protocol = "https:";
  }
  if (protocol !== "http:" && protocol !== "https:") {
    protocol = "https:";
  }

  return {
    protocol,
    host: parsed.hostname,
    port: parsed.port || "",
    basePath: parsed.pathname === "/" ? "" : parsed.pathname.replace(/\/+$/, ""),
  };
}

function buildUpstreamUrl(incomingUrl, target) {
  const upstream = new URL(incomingUrl.toString());
  upstream.protocol = target.protocol;
  upstream.hostname = target.host;
  upstream.port = target.port;
  upstream.pathname = joinPaths(target.basePath, incomingUrl.pathname);
  return upstream;
}

function joinPaths(prefix, suffix) {
  const left = prefix ? prefix.replace(/\/+$/, "") : "";
  const right = suffix ? suffix.replace(/^\/+/, "") : "";
  if (!left && !right) {
    return "/";
  }
  if (!left) {
    return `/${right}`;
  }
  if (!right) {
    return left || "/";
  }
  return `${left}/${right}`;
}

function isWebSocketUpgrade(request) {
  const upgrade = request.headers.get("Upgrade");
  return Boolean(upgrade && upgrade.toLowerCase() === "websocket");
}

function buildProxyHeaders(headers, { websocket, incomingHost, incomingProto }) {
  const outgoing = new Headers(headers);

  // Host is derived from target URL in fetch(); remove any incoming host.
  outgoing.delete("host");
  outgoing.set("x-forwarded-host", incomingHost);
  outgoing.set("x-forwarded-proto", incomingProto.replace(":", ""));

  // Remove Cloudflare edge headers that can confuse some upstreams.
  outgoing.delete("cf-connecting-ip");
  outgoing.delete("cf-ipcountry");
  outgoing.delete("cf-ray");
  outgoing.delete("cf-visitor");

  if (!websocket) {
    for (const h of HOP_BY_HOP_HEADERS) {
      outgoing.delete(h);
    }
  }

  return outgoing;
}

function rewriteLocationHeader(headers, { incomingUrl, upstreamUrl, targetHost }) {
  const rawLocation = headers.get("Location");
  if (!rawLocation) {
    return;
  }

  let location;
  try {
    location = new URL(rawLocation, upstreamUrl.toString());
  } catch {
    return;
  }

  if (location.hostname !== targetHost) {
    return;
  }

  location.protocol = incomingUrl.protocol;
  location.hostname = incomingUrl.hostname;
  location.port = incomingUrl.port;
  headers.set("Location", location.toString());
}

function buildCorsHeaders(request) {
  const origin = request.headers.get("Origin") || "*";
  return new Headers({
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD",
    "Access-Control-Allow-Headers": request.headers.get("Access-Control-Request-Headers") || "*",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin, Access-Control-Request-Headers",
  });
}

function appendCorsHeaders(headers, request) {
  const cors = buildCorsHeaders(request);
  for (const [k, v] of cors.entries()) {
    headers.set(k, v);
  }
}

function json(payload, status = 200, extraHeaders = new Headers()) {
  const headers = new Headers(extraHeaders);
  headers.set("Content-Type", "application/json; charset=utf-8");
  return new Response(JSON.stringify(payload, null, 2), { status, headers });
}
