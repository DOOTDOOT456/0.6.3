// PolyTrack community leaderboard API as a Supabase Edge Function (Deno).
// Preserves the game client's "v6" HTTP protocol exactly.
// Route: https://<ref>.supabase.co/functions/v1/v6/<endpoint>

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);

const TOKEN_RE = /^[A-Za-z0-9]{1,128}$/;
const TRACK_ID_RE = /^[A-Za-z0-9_]{1,80}$/;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "*",
};

function json(obj: unknown, status = 200): Response {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}

function empty(status = 200): Response {
  return new Response(null, { status, headers: CORS });
}

async function userHash(token: string): Promise<string> {
  const data = new TextEncoder().encode(token);
  const digest = await crypto.subtle.digest("SHA-256", data);
  const hex = Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return hex.slice(0, 32);
}

async function upsertUser(userId: string, nickname: string, country: string | null, carStyle: string) {
  await supabase.from("lb_users").upsert(
    { user_id: userId, nickname, country_code: country, car_style: carStyle },
    { onConflict: "user_id" },
  );
}

async function getEntries(trackId: string) {
  const { data, error } = await supabase
    .from("lb_entries")
    .select("id,user_id,nickname,frames,time,car_style,country_code,recording")
    .eq("track_id", trackId)
    .order("frames", { ascending: true })
    .order("id", { ascending: true })
    .limit(1000);
  if (error) throw new Error(error.message);
  return (data ?? []).map((r) => ({
    id: Number(r.id),
    userId: r.user_id,
    nickname: r.nickname,
    frames: Number(r.frames),
    time: r.time,
    carStyle: r.car_style,
    countryCode: r.country_code ?? null,
    recording: r.recording ?? "",
  }));
}

async function handleGet(url: URL): Promise<Response> {
  const endpoint = url.pathname.split("/").pop() ?? "";
  const q = url.searchParams;

  if (endpoint === "leaderboard") {
    const trackId = q.get("trackId") ?? "";
    const skip = parseInt(q.get("skip") ?? "0", 10) || 0;
    const amount = Math.min(parseInt(q.get("amount") ?? "20", 10) || 20, 100);
    const userTokenHash = q.get("userTokenHash") ?? "";

    const entries = await getEntries(trackId);
    const total = entries.length;
    const page = entries.slice(skip, skip + amount);
    let userEntry = null;
    if (userTokenHash) {
      const pos = entries.findIndex((e) => e.userId === userTokenHash);
      if (pos >= 0) {
        userEntry = { position: pos + 1, frames: entries[pos].frames, id: entries[pos].id };
      }
    }
    return json({
      total,
      entries: page.map((e) => ({
        id: e.id,
        userId: e.userId,
        nickname: e.nickname,
        frames: e.frames,
        time: e.time,
        carStyle: e.carStyle,
        verifiedState: 1,
        countryCode: e.countryCode,
      })),
      userEntry,
    });
  }

  if (endpoint === "leaderboardUserEntry") {
    const trackId = q.get("trackId") ?? "";
    const tokenHash = q.get("userTokenHash") ?? "";
    const entries = await getEntries(trackId);
    const pos = entries.findIndex((e) => e.userId === tokenHash);
    if (pos < 0) return json(null);
    const e = entries[pos];
    return json({ position: pos + 1, frames: e.frames, id: e.id });
  }

  if (endpoint === "recordings") {
    const ids = (q.get("ids") ?? "").split(",").filter(Boolean);
    const out: unknown[] = [];
    for (const raw of ids) {
      const id = parseInt(raw, 10);
      if (!Number.isSafeInteger(id)) { out.push(null); continue; }
      const { data, error } = await supabase
        .from("lb_entries")
        .select("recording,frames,car_style")
        .eq("id", id)
        .limit(1);
      if (error || !data || data.length === 0) { out.push(null); continue; }
      const r = data[0];
      out.push({
        recording: r.recording,
        verifiedState: 1,
        frames: Number(r.frames),
        carStyle: r.car_style,
      });
    }
    return json(out);
  }

  if (endpoint === "user") {
    const token = q.get("userToken") ?? "";
    const userId = await userHash(token);
    const { data } = await supabase
      .from("lb_users")
      .select("nickname,country_code,car_style")
      .eq("user_id", userId)
      .limit(1);
    if (!data || data.length === 0) return json(null);
    const u = data[0];
    return json({
      nickname: u.nickname,
      countryCode: u.country_code ?? null,
      carStyle: u.car_style ?? "",
      isVerifier: false,
    });
  }

  if (endpoint === "iceServers") return json([]);

  if (endpoint === "trackOfTheWeek") {
    return json({ serverTime: new Date().toISOString().replace(/\.\d+Z$/, "Z"), current: null });
  }

  return json({ error: "not found" }, 404);
}

async function handlePost(url: URL, form: URLSearchParams): Promise<Response> {
  const endpoint = url.pathname.split("/").pop() ?? "";

  if (endpoint === "user") {
    const token = form.get("userToken") ?? "";
    const nickname = form.get("nickname") ?? "";
    const country = form.get("countryCode"); // may be null
    const carStyle = form.get("carStyle") ?? "";
    if (!TOKEN_RE.test(token) || nickname.length < 1 || nickname.length > 50) return empty(400);
    await upsertUser(await userHash(token), nickname, country ?? null, carStyle);
    return empty(200);
  }

  if (endpoint === "leaderboard") {
    const token = form.get("userToken") ?? "";
    const nickname = form.get("nickname") ?? "";
    const country = form.get("countryCode"); // may be null
    const carStyle = form.get("carStyle") ?? "";
    const trackId = form.get("trackId") ?? "";
    const frames = parseInt(form.get("frames") ?? "0", 10) || 0;
    const recording = form.get("recording") ?? "";
    if (!TOKEN_RE.test(token) || nickname.length < 1 || nickname.length > 50) {
      return json({ error: "bad request" }, 400);
    }
    if (!TRACK_ID_RE.test(trackId)) return json({ error: "bad request" }, 400);
    if (frames <= 0 || recording.length <= 0 || recording.length >= 60000) {
      return json({ error: "bad request" }, 400);
    }

    const userId = await userHash(token);
    await upsertUser(userId, nickname, country ?? null, carStyle);

    const before = await getEntries(trackId);
    const prevIdx = before.findIndex((e) => e.userId === userId);
    const prevPos = prevIdx >= 0 ? prevIdx + 1 : 0;

    const existing = prevIdx >= 0 ? before[prevIdx] : null;
    let entryId: number;
    if (existing) {
      if (frames < existing.frames) {
        const { data, error } = await supabase
          .from("lb_entries")
          .update({
            nickname,
            country_code: country ?? null,
            car_style: carStyle,
            frames,
            time: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
            recording,
          })
          .eq("id", existing.id)
          .select();
        if (error) throw new Error(error.message);
        entryId = existing.id;
      } else {
        entryId = existing.id;
      }
    } else {
      const { data, error } = await supabase
        .from("lb_entries")
        .insert({
          track_id: trackId,
          user_id: userId,
          nickname,
          country_code: country ?? null,
          car_style: carStyle,
          frames,
          time: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
          recording,
        })
        .select();
      if (error) throw new Error(error.message);
      entryId = Number(data![0].id);
    }

    const after = await getEntries(trackId);
    let newPos = after.findIndex((e) => e.id === entryId) + 1;
    if (newPos <= 0) newPos = after.length;

    return json({
      uploadId: entryId,
      previousPosition: prevPos || newPos,
      newPosition: newPos,
    });
  }

  return json({ error: "not found" }, 404);
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

  const url = new URL(req.url);
  try {
    if (req.method === "GET") return await handleGet(url);
    if (req.method === "POST") {
      const body = await req.text();
      return await handlePost(url, new URLSearchParams(body));
    }
    return json({ error: "method not allowed" }, 405);
  } catch (e) {
    console.error("v6 function error:", e);
    return json({ error: "internal" }, 500);
  }
});
