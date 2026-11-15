// Vantage home page: what it is, shown with the live mission.
import { useEffect, useRef, useState } from "react";
import { ArrowDownRight, ArrowRight, CornerDownLeft } from "lucide-react";
import Replay from "./Replay.jsx";
import { LINK_LABEL, NO_SERVICE, clock, num, useMission } from "./lib.jsx";
import { Mark, Wordmark } from "./ui.jsx";

// Measured results for the add-on (sources in the paragraph under the grid).
const MEASURED = [
  {
    label: "People found",
    note: "recall on aerial footage",
    big: "72.4",
    unit: "%",
    bars: [
      { label: "Vantage", value: 72.4 },
      { label: "Stock detector · 36.8%", value: 36.8 },
    ],
  },
  {
    label: "Tracked while the drone moves",
    note: "camera-motion compensation",
    big: "68",
    unit: "%",
    bars: [
      { label: "Vantage", value: 68 },
      { label: "Without compensation · 20%", value: 20 },
    ],
  },
  {
    label: "Results lost when the link drops",
    note: "link cut mid-mission, then restored",
    big: "0",
    unit: "",
    foot:
      "Everything stays on the add-on while there is no signal. When a link returns, the newest version goes up, checksummed; a damaged upload is rejected and sent again.",
  },
  {
    label: "Keeps up with live video",
    note: "full pipeline, on board",
    big: "1×",
    unit: "real time",
    foot: "A 63-second clip, fully processed in 63 seconds on a Jetson Orin Nano. Detection alone: 102 ms per 1080p frame.",
  },
];

const QUESTIONS = [
  "Where is the densest cluster of sightings?",
  "Which sightings have the largest position error?",
  "Summarise this mission for the incident commander.",
];

function useReveal() {
  useEffect(() => {
    const items = document.querySelectorAll(".home [data-reveal]");
    if (!("IntersectionObserver" in window)) {
      items.forEach((el) => el.classList.add("in"));
      return undefined;
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            e.target.classList.add("in");
            io.unobserve(e.target);
          }
        }
      },
      { rootMargin: "0px 0px -8% 0px" },
    );
    items.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);
}

function VersionLanes({ m }) {
  const { uploader, role, manifest, link } = m;
  const local = uploader?.local_version ?? (role === "edge" || role === "demo" ? manifest?.version ?? 0 : null);
  const cloud = uploader?.acked_version ?? (role === "cloud" || role === "demo" ? manifest?.version ?? 0 : null);
  const top = Math.max(local || 0, cloud || 0, 1);
  const first = Math.max(1, top - 23);
  const ids = Array.from({ length: top - first + 1 }, (_, i) => first + i);
  const offline = link === "offline";

  return (
    <div className={`lanes ${offline ? "is-offline" : ""}`}>
      <div className="lane">
        <span className="label">On the drone</span>
        <ol>
          {ids.map((v) => (
            <li key={v} className={local !== null && v <= local ? "on" : ""} title={`snapshot v${v}`}>
              <span className="mono">{v}</span>
            </li>
          ))}
        </ol>
        <span className="lane-value mono">{local !== null ? `v${local}` : "—"}</span>
      </div>
      <div className="lane-link" aria-hidden="true">
        <i />
        <span className={`mono tone-${link}`}>{LINK_LABEL[link]}</span>
      </div>
      <div className="lane">
        <span className="label">In the cloud</span>
        <ol>
          {ids.map((v) => (
            <li
              key={v}
              className={cloud !== null && v <= cloud ? "on sky" : local !== null && v <= local ? "queued" : ""}
              title={`snapshot v${v}`}
            >
              <span className="mono">{v}</span>
            </li>
          ))}
        </ol>
        <span className="lane-value mono">{cloud !== null ? `v${cloud}` : "—"}</span>
      </div>
    </div>
  );
}

export default function Home() {
  const m = useMission();
  const { manifest, summary, people, flightPath, link, role, status } = m;
  const [busy, setBusy] = useState(false);
  const [demoNote, setDemoNote] = useState("");
  const [messages, setMessages] = useState([]);
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const log = useRef(null);
  useReveal();

  useEffect(() => {
    if (log.current) log.current.scrollTop = log.current.scrollHeight;
  }, [messages, asking]);

  async function drive(kind) {
    setBusy(true);
    setDemoNote("");
    try {
      if (kind === "fault") {
        await m.actions.corruptNextUpload();
        setDemoNote("The next upload will arrive cut in half. The cloud rejects it on its checksum; the drone sends it again.");
      } else {
        await m.actions.setOffline(kind === "cut");
        setDemoNote(
          kind === "cut"
            ? "Link cut. Detection and snapshots carry on on board; nothing is lost, uploads wait."
            : "Link back. Only the newest snapshot goes up — older versions are already superseded.",
        );
      }
    } catch (reason) {
      setDemoNote(reason.message);
    } finally {
      setBusy(false);
    }
  }

  async function ask(text) {
    const q = (text ?? question).trim();
    if (!q || asking || !m.canAsk || !m.missionId) return;
    setQuestion("");
    setMessages((list) => [...list, { who: "you", text: q }]);
    setAsking(true);
    try {
      const r = await m.actions.ask(q);
      setMessages((list) => [...list, { who: "vantage", text: r.answer, by: r.generated_by, tools: r.tool_calls || [] }]);
    } catch (reason) {
      setMessages((list) => [...list, { who: "vantage", text: `No answer: ${reason.message}`, by: "error", tools: [] }]);
    } finally {
      setAsking(false);
    }
  }

  const readings = [
    ["Mission", manifest?.mission_id || "—"],
    ["Frames", num(summary.frames_processed)],
    ["Detections", num(summary.detections_processed)],
    ["People placed", num(summary.estimated_people)],
    ["Not placed", num(summary.unlocated_tracks)],
    ["Median error", summary.median_error_m !== undefined ? `±${summary.median_error_m} m` : "—"],
    ["Snapshot", manifest ? `v${manifest.version}` : "—"],
  ];

  const stages = [
    {
      n: "01",
      title: "See",
      body: "The drone's own camera. Video or a recording with its flight log (DJI .SRT) — no special drone needed.",
      value: manifest ? `${num(summary.frames_processed)} frames` : "waiting",
    },
    {
      n: "02",
      title: "Find",
      body: "A Jetson Orin Nano on board runs a person detector trained on aerial footage, in tiles so distant people stay visible.",
      value: manifest ? `${num(summary.detections_processed)} detections` : "waiting",
    },
    {
      n: "03",
      title: "Place",
      body: "Each person is projected to the ground from the camera's pose, with an error radius. Bad pose: kept, unplaced, with the reason.",
      value: manifest ? `${num(summary.estimated_people)} placed · ${num(summary.unlocated_tracks)} not` : "waiting",
    },
    {
      n: "04",
      title: "Keep",
      body: "Every result becomes a versioned, checksummed snapshot on the drone. The map works with no network at all.",
      value: manifest ? `snapshot v${manifest.version}` : "waiting",
    },
    {
      n: "05",
      title: "Send",
      body: "When any link appears — 4G, base Wi-Fi — only the newest snapshot goes to the cloud database.",
      value: LINK_LABEL[link],
      tone: link,
    },
  ];

  return (
    <div className="home">
      <header className="home-nav">
        <Wordmark />
        <nav aria-label="Sections">
          <a href="#how">How it works</a>
          <a href="#link">Offline</a>
          <a href="#measured">Measured</a>
          <a href="#ask">Ask</a>
        </nav>
        <span className={`nav-link-state tone-${link}`}>
          <i className={`dot ${link === "online" || link === "pending" ? "live" : ""}`} />
          <span className="mono">{m.error === NO_SERVICE ? "Website only" : m.error && !status ? "No service" : LINK_LABEL[link]}</span>
        </span>
        <a className="btn btn-signal" href="#/dashboard">
          Mission control <ArrowRight size={15} />
        </a>
      </header>

      <section className="hero">
        <div className="hero-copy">
          <p className="label hero-kicker">Search and rescue · onboard AI · offline first</p>
          <h1 className="hero-title">
            Find the <em>people</em> the signal can&rsquo;t reach.
          </h1>
          <p className="hero-lede">
            Vantage gives an ordinary drone a memory and a pair of eyes. A Jetson on board finds people
            in the video, puts each one on the map with an honest error ring, and keeps every result on
            the drone until any link can carry it home.
          </p>
          <div className="hero-actions">
            <a className="btn btn-signal btn-lg" href="#/dashboard">
              Open mission control <ArrowRight size={16} />
            </a>
            <a className="btn btn-ghost btn-lg" href="#how">
              How it works <ArrowDownRight size={16} />
            </a>
          </div>
        </div>
        <div className="hero-replay">
          <Replay people={people} flightPath={flightPath} manifest={manifest} live={Boolean(status?.running)} />
        </div>
      </section>

      <div className="readings" role="list" aria-label="Live readings">
        {readings.map(([k, v]) => (
          <div role="listitem" key={k}>
            <span className="label">{k}</span>
            <span className="mono">{v}</span>
          </div>
        ))}
      </div>

      <section className="gap" id="how">
        <div className="gap-statement" data-reveal>
          <p className="label">The gap</p>
          <h2>
            Search drones are good at seeing and bad at remembering. They stream video to the ground,
            and when the link drops in a valley, the picture drops with it.
          </h2>
        </div>
        <ol className="stages">
          {stages.map((s, i) => (
            <li key={s.n} data-reveal style={{ "--i": i }}>
              <span className="stage-n mono">{s.n}</span>
              <h3>{s.title}</h3>
              <p>{s.body}</p>
              <span className={`stage-value mono ${s.tone ? `tone-${s.tone}` : ""}`}>{s.value}</span>
            </li>
          ))}
        </ol>
      </section>

      <section className="link-section" id="link">
        <div className="link-head" data-reveal>
          <p className="label">Offline, on purpose</p>
          <h2>Cut the link. Nothing is lost.</h2>
          <p>
            Snapshots pile up on the drone while there is no signal. When the link returns, the cloud
            gets the newest one — the older versions it skipped are already contained in it.
          </p>
        </div>
        <div className="link-board" data-reveal>
          <VersionLanes m={m} />
          <div className="link-controls">
            <button type="button" className="btn btn-ghost" disabled={busy || !m.canDriveLink || link === "offline"} onClick={() => drive("cut")}>
              Cut the link
            </button>
            <button type="button" className="btn btn-ghost" disabled={busy || !m.canDriveLink || link !== "offline"} onClick={() => drive("restore")}>
              Restore it
            </button>
            <button type="button" className="btn btn-ghost" disabled={busy || !m.canCorrupt} onClick={() => drive("fault")}>
              Corrupt the next upload
            </button>
            <p className="link-note" role="status">
              {demoNote ||
                (m.canDriveLink || m.canCorrupt
                  ? "These controls act on the real edge pipeline."
                  : "The controls come alive when Vantage runs from the Jetson (or the edge demo) with an uplink configured.")}
            </p>
          </div>
        </div>
      </section>

      <section className="measured" id="measured">
        <div className="measured-head" data-reveal>
          <p className="label">Measured, not promised</p>
          <h2>One box turns any drone into a search instrument.</h2>
          <p>
            Vantage sits between the camera and the rescue team. It brings the onboard AI the drone
            doesn&rsquo;t have, the memory it doesn&rsquo;t have, and the sync it can&rsquo;t do without
            signal. No new drone, no ground link.
          </p>
        </div>
        <div className="measured-grid">
          {MEASURED.map((row) => (
            <div className="metric" key={row.label} data-reveal>
              <div className="metric-top">
                <h3>{row.label}</h3>
                <span className="label">{row.note}</span>
              </div>
              <div className="metric-big mono">
                {row.big}
                {row.unit && <small>{row.unit}</small>}
              </div>
              {row.bars ? (
                <div className="bars">
                  {row.bars.map((b, i) => (
                    <div key={b.label} className={`bar ${i === 0 ? "ours" : "stock"}`} style={{ "--w": `${b.value}%` }}>
                      <span>{b.label}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="metric-foot">{row.foot}</p>
              )}
            </div>
          ))}
        </div>
        <p className="measured-source">
          People found: 60 VisDrone validation images, 640 px tiles (these images also chose the training
          epoch, so read it as approximate). Tracking: 2,292 labelled people in moving aerial sequences,
          on the Jetson. Link outage, real time and speed: end-to-end runs on the Jetson with uploads to
          Tiger Data.
        </p>
      </section>

      <section className="ask" id="ask">
        <div className="ask-head" data-reveal>
          <p className="label">Ask the mission</p>
          <h2>Questions in plain words, answers from the data.</h2>
          <p>
            The cloud service answers with Gemini and a fixed set of mission tools — it reads the
            sightings, it does not invent them. Every answer is an estimate for a person to check.
          </p>
        </div>
        <div className="ask-box" data-reveal>
          <div className="ask-log" ref={log} aria-live="polite">
            {!messages.length && (
              <div className="ask-empty">
                {m.canAsk ? (
                  <>
                    <span className="label">Try</span>
                    {(role === "demo" ? m.savedAnswers.map((a) => a.question) : QUESTIONS).map((q) => (
                      <button type="button" key={q} onClick={() => ask(q)} disabled={!m.missionId}>
                        {q}
                        <ArrowRight size={14} />
                      </button>
                    ))}
                  </>
                ) : (
                  <p>
                    {role === "demo"
                      ? "Questions are answered by the cloud service when Vantage is connected to it."
                      : `Questions are answered by the cloud API. You are looking at ${
                          role === "edge" ? "the drone's own copy" : "a page with no service behind it"
                        }; open Vantage through the cloud address to ask.`}
                  </p>
                )}
              </div>
            )}
            {messages.map((msg, i) => (
              <div key={i} className={`msg msg-${msg.who}`}>
                <span className="label">{msg.who === "you" ? "You" : msg.by === "gemini" ? "Vantage · Gemini" : "Vantage"}</span>
                <p>{msg.text}</p>
                {msg.tools?.length > 0 && (
                  <span className="msg-tools mono">used {[...new Set(msg.tools.map((t) => t.tool || t.name || t))].join(", ")}</span>
                )}
              </div>
            ))}
            {asking && <div className="msg msg-vantage pending"><span className="label">Vantage</span><p>Reading the mission…</p></div>}
          </div>
          {m.canAsk && messages.length > 0 && (
            <div className="ask-chips">
              {(role === "demo" ? m.savedAnswers.map((a) => a.question) : QUESTIONS).map((q) => (
                <button type="button" key={q} onClick={() => ask(q)} disabled={asking}>
                  {q}
                </button>
              ))}
            </div>
          )}
          <form
            className="ask-input"
            onSubmit={(e) => {
              e.preventDefault();
              ask();
            }}
          >
            <input
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder={
                role === "cloud"
                  ? "Ask about this mission"
                  : m.canAsk
                    ? "Pick a question above"
                    : "Available on the cloud service"
              }
              disabled={!m.canAsk || asking}
              aria-label="Question about the mission"
            />
            <button type="submit" disabled={!m.canAsk || asking || !question.trim()} aria-label="Ask">
              <CornerDownLeft size={16} />
            </button>
          </form>
        </div>
      </section>

      <footer className="home-foot">
        <div className="foot-row">
          <p>
            Decision support. Positions and counts are estimates. A person reviews every result before
            anyone is sent anywhere.
          </p>
          <div className="foot-meta mono">
            <span>{manifest ? `${manifest.mission_id} · v${manifest.version} · ${clock(manifest.created_at)}` : "No mission loaded"}</span>
            <a href="#/dashboard">Mission control →</a>
          </div>
        </div>
        <div className="foot-mark" aria-hidden="true">
          <Mark size={64} />
          <span>VANTAGE</span>
        </div>
      </footer>
    </div>
  );
}
