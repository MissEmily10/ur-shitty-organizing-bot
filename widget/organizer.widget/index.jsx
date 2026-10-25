// 🖥 Виджет «Мой день» для Übersicht (https://tracesof.net/uebersicht/): расписание на день и планы из бота.
// Адрес бота вписывается сам, когда бот присылает этот файл командой /widget. Ничего здесь менять не нужно.
// «＋» спрашивает через обычные окна macOS: что и когда, что это, разово ли. «☐» закрывает задачу.

import { css, run } from "uebersicht";

const BOT = "__BOT_URL__";
export const refreshFrequency = 5 * 60 * 1000; // каждые 5 минут
// куда передвинут виджет (сдвиг от правого верхнего угла) — запоминается на этом Mac
const savedOffset = () => {
  try {
    return JSON.parse(localStorage.getItem("organizer.offset")) || { x: 0, y: 0 };
  } catch (e) {
    return { x: 0, y: 0 };
  }
};

export const initialState = { data: null, error: "", busy: "", allPlans: false, tint: null, offset: savedOffset() };

let shownDate = ""; // какой день открыт стрелками; пусто — бот сам решает (сегодня, а вечером — завтра)

const load = (dispatch) =>
  fetch(`${BOT}/day${shownDate ? `?d=${shownDate}` : ""}`)
    .then((r) => r.json())
    .then((data) => dispatch(data.error ? { type: "ERROR", error: data.error } : { type: "DATA", data }))
    .catch(() => dispatch({ type: "ERROR", error: "Нет связи с ботом — проверю через 5 минут" }));

// ---------- стекло под цвет обоев: средний цвет картинки рабочего стола (раз в 30 минут) ----------

// wallpaper.sh лежит рядом с этим файлом: средний цвет обоев или ERR:причина
const WALLPAPER = "if [ -f organizer.widget/wallpaper.sh ]; then bash organizer.widget/wallpaper.sh; else bash wallpaper.sh; fi";
let tintAt = 0;

const byte = (hex, at) => parseInt(hex.substr(at * 2, 2), 16);
const word = (hex, at) => byte(hex, at) + byte(hex, at + 1) * 256 + byte(hex, at + 2) * 65536;

const loadTint = (dispatch) => {
  if (Date.now() - tintAt < 10 * 60 * 1000) return; // поменяла обои — виджет подстроится за 10 минут
  tintAt = Date.now();
  run(WALLPAPER)
    .then((hex) => {
      hex = hex.trim();
      if (hex.startsWith("ERR:") || hex.length < 60) {
        // обои не нашлись (живые обои, смена фото…) — нейтральное стекло, а причину видно внизу виджета
        return dispatch({ type: "TINT_ERROR", error: hex.startsWith("ERR:") ? hex.slice(4) : "не получила цвет обоев" });
      }
      const at = word(hex, 10); // в BMP пиксели начинаются по адресу из заголовка, цвет записан как B, G, R
      dispatch({ type: "TINT", tint: [byte(hex, at + 2), byte(hex, at + 1), byte(hex, at)] });
    })
    .catch((e) => dispatch({ type: "TINT_ERROR", error: `не запустилась проверка обоев (${String(e).slice(0, 80)})` }));
};

export const command = (dispatch) => {
  loadTint(dispatch);
  return load(dispatch);
};

// Палитра карточек под обои: оттенок «уроков» = оттенок обоев, остальные типы повернуты на те же углы, что в палитре
// из Figma (репетиторы +62°, вне школы −171°, события +180°). Светлота подобрана под контраст: на светлом стекле —
// светлые пастельные карточки с тёмным текстом, на тёмном — тёмные карточки со светлым текстом.
const FIGMA = { lesson: 0, tutor: 62, other: -171, event: 180 };

const hsl = (r, g, b) => {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min, s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  const h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return [h * 60, s, l];
};

const palette = (tint, dark) => {
  let [h, s] = tint ? hsl(...tint) : [242, 1];
  if (s < 0.12) h = 242; // серые обои — оттенки из Figma как есть
  const box = (shift, sat = 1) => {
    const hue = (((h + shift) % 360) + 360) % 360;
    return dark
      ? [`hsl(${hue}, ${Math.round(38 * sat)}%, 27%)`, `hsl(${hue}, ${Math.round(70 * sat)}%, 72%)`, "#F7F5F2", "rgba(247, 245, 242, 0.72)"]
      : [`hsl(${hue}, ${Math.round(90 * sat)}%, 90%)`, `hsl(${hue}, ${Math.round(55 * sat)}%, 42%)`, "#1F1F24", "rgba(31, 31, 36, 0.66)"];
  };
  return {
    lesson: box(FIGMA.lesson), tutor: box(FIGMA.tutor), other: box(FIGMA.other), event: box(FIGMA.event, 0.6),
    routine: box(0, 0.12), note: box(0, 0.12),
  };
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
    dark,
  };
};

export const updateState = (event, state) => {
  switch (event.type) {
    case "DATA":
      return { ...state, data: event.data, error: "", busy: "" };
    case "ERROR":
      return { ...state, error: event.error, busy: "" };
    case "TINT":
      return { ...state, tint: event.tint, tintError: "" };
    case "TINT_ERROR":
      return { ...state, tintError: event.error };
    case "BUSY":
      return { ...state, busy: event.text };
    case "MOVE":
      return { ...state, offset: event.offset };
    case "PLANS":
      return { ...state, data: { ...state.data, plans: event.plans } };
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

// «⏰ завтра», перетаскивание планов и настроение
const later = async (dispatch, id) => {
  dispatch({ type: "BUSY", text: "Переношу на завтра…" });
  const res = await send("tomorrow", { id }).catch(() => ({ error: "Нет связи с ботом" }));
  if (res.error) return dispatch({ type: "ERROR", error: res.error });
  load(dispatch);
};

let dragId = "";

const drop = (dispatch, plans, targetId) => {
  if (!dragId || dragId === targetId) return;
  const moving = plans.find((p) => p.id === dragId);
  const rest = plans.filter((p) => p.id !== dragId);
  rest.splice(rest.findIndex((p) => p.id === targetId), 0, moving);
  dragId = "";
  dispatch({ type: "PLANS", plans: rest }); // сразу на экране, потом — боту
  send("order", { ids: rest.map((p) => p.id) }).catch(() => dispatch({ type: "ERROR", error: "Порядок не сохранился — нет связи" }));
};

const rate = async (dispatch, score) => {
  dispatch({ type: "BUSY", text: "Записываю настроение…" });
  const res = await send("mood", { score }).catch(() => ({ error: "Нет связи с ботом" }));
  if (res.error) return dispatch({ type: "ERROR", error: res.error });
  load(dispatch);
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
    color: var(--box-muted);
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
    border: 1px solid var(--box-border);
    border-radius: 10px;
    padding: 0 8px;
    cursor: pointer;
    background: transparent;
    color: inherit;
  }
`;
const gap = css`
  margin: 2px 0 8px 52px;
  font-size: 11px;
  color: var(--muted);
  border-left: 2px dashed var(--line);
  padding-left: 8px;
`;
const mood = css`
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--muted);
  button {
    border: 1px solid var(--line);
    background: var(--pill);
    color: var(--text);
    border-radius: 12px;
    min-width: 30px;
    padding: 2px 8px;
    font: inherit;
    font-weight: 700;
    cursor: pointer;
  }
  button.on {
    border: 2px solid var(--accent);
    transform: scale(1.12);
  }
`;
const MOODS = ["😣", "😕", "😐", "🙂", "😄"];

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
  .grip {
    cursor: grab;
    color: var(--muted);
    font-size: 12px;
    margin-top: 1px;
    user-select: none;
  }
  position: relative;
  .later {
    position: absolute;
    right: 0;
    top: 5px;
    font-size: 10px;
    font-weight: 700;
    border: 1px solid var(--line);
    background: var(--pill);
    border-radius: 10px;
    padding: 1px 8px;
    cursor: pointer;
    opacity: 0;
    white-space: nowrap;
  }
  &:hover .later {
    opacity: 1;
  }
  &.over {
    box-shadow: 0 -2px 0 var(--accent);
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

const Day = ({ data, dispatch, colors }) => {
  const now = minutes(data.now);
  const nextIndex = data.today ? data.items.findIndex((i) => minutes(i.start) > now) : -1;
  const out = [];
  data.items.forEach((item, k) => {
    const prev = data.items[k - 1];
    if (prev && prev.end && item.start && minutes(item.start) - minutes(prev.end) >= 30) {
      out.push(<div className={gap} key={`gap${k}`}>окно {pause(minutes(item.start) - minutes(prev.end))}</div>);
    }
    const [fill, border, text, muted] = colors[item.kind] || colors.note;
    const current = data.today && item.start && item.end && minutes(item.start) <= now && now < minutes(item.end);
    const meta = [item.end ? `до ${item.end}` : "", item.online ? "онлайн" : "", item.teacher, item.once ? "разово" : ""].filter(Boolean).join(" · ");
    out.push(
      <div className={row} key={k}>
        <div className="time">{item.start || "весь день"}</div>
        <div className="box" style={{ background: fill, borderColor: border, borderWidth: current ? 2 : 1, color: text,
                                      "--box-muted": muted, "--box-border": border }}>
          {current && <span className="badge" style={{ color: border }}>сейчас</span>}
          {k === nextIndex && !current && <span className="badge" style={{ color: muted }}>дальше</span>}
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
          <div className={plan} key={p.id} draggable
               onDragStart={() => { dragId = p.id; }}
               onDragOver={(e) => { e.preventDefault(); e.currentTarget.classList.add("over"); }}
               onDragLeave={(e) => e.currentTarget.classList.remove("over")}
               onDrop={(e) => { e.currentTarget.classList.remove("over"); drop(dispatch, plans, p.id); }}>
            <div className="grip" title="Перетащи, чтобы поменять важность">⋮⋮</div>
            <div className="check" title="Закрыть" onClick={() => close(dispatch, p.id)} />
            <div style={{ flex: 1 }}>
              <div className="t">
                {p.title} <span style={{ color, fontSize: 10, fontWeight: 700, textTransform: "uppercase" }}>{label}</span>
                <span className="later" title="Срок — завтра" onClick={() => later(dispatch, p.id)}>⏰ завтра</span>
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
// перетаскивание за ⠿ в левом верхнем углу: двигаем, отпустили — место запомнилось
const startMove = (event, offset, dispatch) => {
  event.preventDefault();
  const start = { x: event.clientX - offset.x, y: event.clientY - offset.y };
  let last = offset;
  const move = (e) => {
    last = { x: e.clientX - start.x, y: e.clientY - start.y };
    dispatch({ type: "MOVE", offset: last });
  };
  const stop = () => {
    window.removeEventListener("mousemove", move);
    window.removeEventListener("mouseup", stop);
    try {
      localStorage.setItem("organizer.offset", JSON.stringify(last));
    } catch (e) {}
  };
  window.addEventListener("mousemove", move);
  window.addEventListener("mouseup", stop);
};

const handle = css`
  position: absolute;
  top: 6px;
  left: 50%;
  transform: translateX(-50%);
  width: 46px;
  height: 6px;
  border-radius: 3px;
  background: var(--line);
  cursor: grab;
  &:hover {
    background: var(--muted);
  }
`;

export const render = ({ data, error, busy, allPlans, tint, tintError, offset }, dispatch) => {
  const { dark, ...vars } = theme(tint);
  const colors = palette(tint, dark);
  return (
  <div style={{ ...vars, color: "var(--text)", position: "relative", transform: `translate(${offset.x}px, ${offset.y}px)` }}>
    <style>{"@import url('https://fonts.googleapis.com/css2?family=Paytone+One&family=PT+Sans:wght@400;700&display=swap');"}</style>
    <div className={card} style={{ position: "relative" }}>
      <div className={handle} title="Потяни, чтобы передвинуть виджет" onMouseDown={(e) => startMove(e, offset, dispatch)} />
      <section>
        <div className={head}>
          <button className={btn} onClick={() => data && shift(dispatch, data, -1)}>‹</button>
          <h1>{data ? data.title : "Мой день"}</h1>
          {data && data.relative && <small>{data.relative}</small>}
          <button className={btn} onClick={() => data && shift(dispatch, data, 1)}>›</button>
          <button className={btn} title="Добавить в расписание" onClick={() => addEvent(dispatch)}>＋</button>
        </div>
        {data && data.mood && data.mood.ask && (
          <div className={mood}>
            <span>{data.mood.score ? "Настроение сегодня:" : "Как прошёл день?"}</span>
            {MOODS.map((face, k) => (
              <button key={k} className={data.mood.score === k + 1 ? "on" : ""} title={`${k + 1}`} onClick={() => rate(dispatch, k + 1)}>
                {face}
              </button>
            ))}
          </div>
        )}
        {data ? <Day data={data} dispatch={dispatch} colors={colors} /> : <div className={note}>Загружаю…</div>}
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
        {tintError && <div className={note}>🎨 Цвет под обои: {tintError}</div>}
      </section>
    </div>
  </div>
);
};
