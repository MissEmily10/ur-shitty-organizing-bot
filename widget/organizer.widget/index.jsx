// 🖥 Виджет «Мой день» для Übersicht (https://tracesof.net/uebersicht/): расписание на день и планы из бота.
// Адрес бота вписывается сам, когда бот присылает этот файл командой /widget. Ничего здесь менять не нужно.
// «＋» спрашивает через обычные окна macOS: что и когда, что это, разово ли. «☐» закрывает задачу.

import { css, run } from "uebersicht";

const BOT = "__BOT_URL__";
export const refreshFrequency = 5 * 60 * 1000; // каждые 5 минут
export const initialState = { data: null, error: "", busy: "", allPlans: false, tint: null };

let shownDate = ""; // какой день открыт стрелками; пусто — бот сам решает (сегодня, а вечером — завтра)

const load = (dispatch) =>
  fetch(`${BOT}/day${shownDate ? `?d=${shownDate}` : ""}`)
    .then((r) => r.json())
    .then((data) => dispatch(data.error ? { type: "ERROR", error: data.error } : { type: "DATA", data }))
    .catch(() => dispatch({ type: "ERROR", error: "Нет связи с ботом — проверю через 5 минут" }));

// ---------- стекло под цвет обоев: средний цвет картинки рабочего стола (раз в 30 минут) ----------

const WALLPAPER = [
  `f=$(osascript -e 'tell application "System Events" to get picture of current desktop' 2>/dev/null)`,
  `[ -f "$f" ] && sips -s format bmp -z 1 1 "$f" --out /tmp/organizer-wallpaper.bmp >/dev/null 2>&1`,
  `&& xxd -p /tmp/organizer-wallpaper.bmp | tr -d '\\n'`,
].join(" ");
let tintAt = 0;

const byte = (hex, at) => parseInt(hex.substr(at * 2, 2), 16);
const word = (hex, at) => byte(hex, at) + byte(hex, at + 1) * 256 + byte(hex, at + 2) * 65536;

const loadTint = (dispatch) => {
  if (Date.now() - tintAt < 30 * 60 * 1000) return;
  tintAt = Date.now();
  run(WALLPAPER)
    .then((hex) => {
      hex = hex.trim();
      if (hex.length < 60) return; // обои — видео или их не видно: остаётся нейтральное стекло
      const at = word(hex, 10); // в BMP пиксели начинаются по адресу из заголовка, цвет записан как B, G, R
      dispatch({ type: "TINT", tint: [byte(hex, at + 2), byte(hex, at + 1), byte(hex, at)] });
    })
    .catch(() => {});
};

export const command = (dispatch) => {
  loadTint(dispatch);
  return load(dispatch);
};

const theme = (tint) => {
  const [r, g, b] = tint || [255, 255, 255];
  const dark = (0.299 * r + 0.587 * g + 0.114 * b) / 255 < 0.42;
  const mix = (c, to, k) => Math.round(c + (to - c) * k);
  const glass = dark
    ? `rgba(${mix(r, 0, 0.45)}, ${mix(g, 0, 0.45)}, ${mix(b, 0, 0.45)}, 0.42)`
    : `rgba(${mix(r, 255, 0.55)}, ${mix(g, 255, 0.55)}, ${mix(b, 255, 0.55)}, 0.42)`;
  return {
    "--glass": glass,
    "--text": dark ? "#F7F5F2" : "#1F1F24",
    "--muted": dark ? "rgba(255, 255, 255, 0.66)" : "rgba(31, 31, 36, 0.58)",
    "--pill": dark ? "rgba(255, 255, 255, 0.14)" : "rgba(255, 255, 255, 0.5)",
    "--line": dark ? "rgba(255, 255, 255, 0.14)" : "rgba(31, 31, 36, 0.09)",
    "--accent": `rgb(${mix(r, dark ? 255 : 0, 0.25)}, ${mix(g, dark ? 255 : 0, 0.25)}, ${mix(b, dark ? 255 : 0, 0.25)})`,
  };
};

export const updateState = (event, state) => {
  switch (event.type) {
    case "DATA":
      return { ...state, data: event.data, error: "", busy: "" };
    case "ERROR":
      return { ...state, error: event.error, busy: "" };
    case "TINT":
      return { ...state, tint: event.tint };
    case "BUSY":
      return { ...state, busy: event.text };
    case "TOGGLE_PLANS":
      return { ...state, allPlans: !state.allPlans };
    default:
      return state;
  }
};

// ---------- окна macOS для «＋» ----------

const ask = (script) => run(`osascript ${script.map((line) => `-e '${line}'`).join(" ")}`).then((out) => out.trim());

const askText = (title, prompt) =>
  ask([`display dialog "${prompt}" default answer "" with title "${title}" buttons {"Отмена", "Готово"} default button 2`, "text returned of result"]);

const choose = (title, items) =>
  ask([`choose from list {${items.map((i) => `"${i}"`).join(", ")}} with title "${title}" default items {"${items[0]}"}`]).then((out) => {
    if (!out || out === "false") throw new Error("cancel");
    return out;
  });

const KINDS = { Репетитор: "tutor", "Засечка под личные дела": "me", "Доп. урок": "lesson", "Доп. дело": "task" };

const send = (path, body) =>
  fetch(`${BOT}/${path}`, { method: "POST", headers: { "Content-Type": "text/plain" }, body: JSON.stringify(body) }).then((r) => r.json());

const addEvent = async (dispatch) => {
  try {
    const text = await askText("Новое в расписании", "Что и когда? Например: в пятницу 19:00–20:00 китайский");
    if (!text) return;
    const kind = KINDS[await choose("Что это?", Object.keys(KINDS))];
    const once = (await choose("Разово или каждую неделю?", ["Разово", "Каждую неделю"])) === "Разово";
    dispatch({ type: "BUSY", text: "Добавляю…" });
    const res = await send("add", { text, kind, once });
    if (res.error) return dispatch({ type: "ERROR", error: res.error });
    load(dispatch);
  } catch (e) {
    /* нажали «Отмена» — ничего не делаем */
  }
};

const addNote = async (dispatch) => {
  try {
    const text = await askText("Новая заметка", "Что записать? Как сообщение боту — срок можно словами");
    if (!text) return;
    dispatch({ type: "BUSY", text: "Сохраняю…" });
    const res = await send("add", { text, kind: "note" });
    if (res.error) return dispatch({ type: "ERROR", error: res.error });
    load(dispatch);
  } catch (e) {}
};

const close = async (dispatch, id) => {
  dispatch({ type: "BUSY", text: "Закрываю…" });
  const res = await send("done", { id }).catch(() => ({ error: "Нет связи с ботом" }));
  if (res.error) return dispatch({ type: "ERROR", error: res.error });
  load(dispatch);
};

const shift = (dispatch, data, days) => {
  const d = new Date(`${data.date}T12:00:00`);
  d.setDate(d.getDate() + days);
  shownDate = d.toISOString().slice(0, 10);
  load(dispatch);
};

const openLink = (link) => {
  if (/^https:\/\/[^\s'"]+$/.test(link)) run(`open '${link}'`);
};

// ---------- оформление: палитра «Моего расписания» из Figma ----------

const COLORS = {
  lesson: ["#D5D4FF", "#9B4DDB"],
  tutor: ["#FFE1FD", "#9B4DDB"],
  other: ["#F5FFCB", "#A37E2F"],
  event: ["#FFF4E6", "#A37E2F"],
  routine: ["#F2F2F6", "#8A8A95"],
  note: ["#F2F2F6", "#8A8A95"],
};
const URGENCY = { fire: ["горит", "#E5534B"], soon: ["скоро", "var(--text)"], later: ["потом", "var(--muted)"] };

export const className = css`
  top: 24px;
  right: 24px;
  width: 760px;
  font-family: "PT Sans", -apple-system, sans-serif;
  color: var(--text);
  -webkit-font-smoothing: antialiased;
`;

const card = css`
  background: var(--glass);
  -webkit-backdrop-filter: blur(28px) saturate(160%);
  backdrop-filter: blur(28px) saturate(160%);
  border: 1px solid var(--line);
  border-radius: 18px;
  box-shadow: 0 8px 30px rgba(31, 31, 36, 0.12);
  padding: 16px 16px 12px;
  display: flex;
  gap: 18px;
  align-items: flex-start;
  & > section {
    flex: 1;
    min-width: 0;
  }
  & > section + section {
    border-left: 1px solid var(--line);
    padding-left: 18px;
  }
`;
const head = css`
  display: flex;
  align-items: center;
  background: var(--pill);
  border-radius: 8px;
  padding: 6px 10px;
  margin-bottom: 10px;
  h1 {
    font-family: "Paytone One", "PT Sans", sans-serif;
    font-size: 17px;
    font-weight: normal;
    margin: 0;
    flex: 1;
    letter-spacing: -0.02em;
  }
  small {
    font-size: 11px;
    color: var(--muted);
    margin-right: 8px;
  }
`;
const btn = css`
  border: none;
  background: transparent;
  font-size: 16px;
  cursor: pointer;
  color: var(--text);
  padding: 0 4px;
  &:hover {
    color: var(--accent);
  }
`;
const row = css`
  display: flex;
  gap: 10px;
  margin-bottom: 6px;
  .time {
    width: 42px;
    font-size: 12px;
    font-weight: 700;
    padding-top: 6px;
    text-align: right;
    color: var(--text);
  }
  .box {
    flex: 1;
    border-radius: 6px;
    padding: 5px 9px 6px;
    border: 1px solid;
  }
  .title {
    font-size: 13.5px;
    font-weight: 700;
  }
  .meta {
    font-size: 11px;
    color: #5a5a66;
  }
  .badge {
    float: right;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    margin-left: 6px;
  }
  .join {
    float: right;
    font-size: 11px;
    border: 1px solid #9b4ddb;
    border-radius: 10px;
    padding: 0 8px;
    cursor: pointer;
    background: #fff;
  }
`;
const gap = css`
  margin: 2px 0 8px 52px;
  font-size: 11px;
  color: var(--muted);
  border-left: 2px dashed var(--line);
  padding-left: 8px;
`;
const plan = css`
  display: flex;
  gap: 8px;
  padding: 5px 0;
  border-top: 1px solid var(--line);
  .check {
    width: 14px;
    height: 14px;
    border: 1.5px solid var(--text);
    border-radius: 3px;
    margin-top: 2px;
    cursor: pointer;
    flex: none;
    &:hover {
      background: #d5d4ff;
    }
  }
  .t {
    font-size: 13px;
    font-weight: 700;
  }
  .w {
    font-size: 11px;
    color: var(--muted);
  }
  .step {
    display: flex;
    gap: 6px;
    font-size: 12px;
    margin-top: 3px;
  }
  .step .check {
    width: 11px;
    height: 11px;
  }
`;
const note = css`
  font-size: 11px;
  color: var(--muted);
  margin-top: 6px;
`;

const minutes = (hhmm) => (hhmm ? Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5)) : 0);
const pause = (m) => (m >= 60 ? `${Math.floor(m / 60)} ч${m % 60 ? ` ${m % 60} мин` : ""}` : `${m} мин`);

const Day = ({ data, dispatch }) => {
  const now = minutes(data.now);
  const nextIndex = data.today ? data.items.findIndex((i) => minutes(i.start) > now) : -1;
  const out = [];
  data.items.forEach((item, k) => {
    const prev = data.items[k - 1];
    if (prev && prev.end && item.start && minutes(item.start) - minutes(prev.end) >= 30) {
      out.push(<div className={gap} key={`gap${k}`}>окно {pause(minutes(item.start) - minutes(prev.end))}</div>);
    }
    const [fill, border] = COLORS[item.kind] || COLORS.note;
    const current = data.today && item.start && item.end && minutes(item.start) <= now && now < minutes(item.end);
    const meta = [item.end ? `до ${item.end}` : "", item.online ? "онлайн" : "", item.teacher, item.once ? "разово" : ""].filter(Boolean).join(" · ");
    out.push(
      <div className={row} key={k}>
        <div className="time">{item.start || "весь день"}</div>
        <div className="box" style={{ background: `${fill}E6`, borderColor: border, borderWidth: current ? 2 : 1, color: "#1F1F24" }}>
          {current && <span className="badge" style={{ color: "#9B4DDB" }}>сейчас</span>}
          {k === nextIndex && !current && <span className="badge" style={{ color: "#8A8A95" }}>дальше</span>}
          {item.link && <span className="join" onClick={() => openLink(item.link)}>войти</span>}
          <div className="title">{item.title}</div>
          {meta && <div className="meta">{meta}</div>}
        </div>
      </div>
    );
  });
  return out.length ? out : <div className={note}>Свободный день 🌿</div>;
};

const Plans = ({ plans, all, dispatch }) => {
  const shown = all ? plans : plans.slice(0, 12);
  return (
    <div>
      {shown.map((p) => {
        const [label, color] = URGENCY[p.urgency];
        return (
          <div className={plan} key={p.id}>
            <div className="check" title="Закрыть" onClick={() => close(dispatch, p.id)} />
            <div style={{ flex: 1 }}>
              <div className="t">
                {p.title} <span style={{ color, fontSize: 10, fontWeight: 700, textTransform: "uppercase" }}>{label}</span>
              </div>
              <div className="w">{[p.when && `до ${p.when}`, p.where].filter(Boolean).join(" · ")}</div>
              {p.steps.map((s) => (
                <div className="step" key={s.id}>
                  <div className="check" title="Закрыть" onClick={() => close(dispatch, s.id)} />
                  {s.title}
                </div>
              ))}
            </div>
          </div>
        );
      })}
      {plans.length > 12 && (
        <div className={note} style={{ cursor: "pointer" }} onClick={() => dispatch({ type: "TOGGLE_PLANS" })}>
          {all ? "свернуть" : `ещё ${plans.length - 12}…`}
        </div>
      )}
    </div>
  );
};

// один большой виджет: слева — день, справа — планы
export const render = ({ data, error, busy, allPlans, tint }, dispatch) => (
  <div style={{ ...theme(tint), color: "var(--text)" }}>
    <style>{"@import url('https://fonts.googleapis.com/css2?family=Paytone+One&family=PT+Sans:wght@400;700&display=swap');"}</style>
    <div className={card}>
      <section>
        <div className={head}>
          <button className={btn} onClick={() => data && shift(dispatch, data, -1)}>‹</button>
          <h1>{data ? data.title : "Мой день"}</h1>
          {data && data.relative && <small>{data.relative}</small>}
          <button className={btn} onClick={() => data && shift(dispatch, data, 1)}>›</button>
          <button className={btn} title="Добавить в расписание" onClick={() => addEvent(dispatch)}>＋</button>
        </div>
        {data ? <Day data={data} dispatch={dispatch} /> : <div className={note}>Загружаю…</div>}
        {data && !data.today && (
          <div className={note} style={{ cursor: "pointer" }} onClick={() => { shownDate = ""; load(dispatch); }}>
            ↩︎ к сегодня
          </div>
        )}
      </section>
      <section>
        <div className={head}>
          <h1>Plans</h1>
          {data && <small>{data.plans.length}</small>}
          <button className={btn} title="Новая заметка" onClick={() => addNote(dispatch)}>＋</button>
        </div>
        {data ? <Plans plans={data.plans} all={allPlans} dispatch={dispatch} /> : null}
        {(busy || error) && <div className={note}>{busy || error}</div>}
      </section>
    </div>
  </div>
);
