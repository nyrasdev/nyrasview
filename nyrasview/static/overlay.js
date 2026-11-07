(() => {
  if (window.__nyra) return;
  window.__nyra = true;
  const PARENT = "__NYRA_PARENT__";
  const host = document.createElement("div");
  host.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `<style>
    :host{all:initial}
    *{box-sizing:border-box;font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
    .box{position:fixed;border:2px solid var(--c);border-radius:10px;box-shadow:0 0 0 4px color-mix(in srgb,var(--c) 18%,transparent),0 0 24px color-mix(in srgb,var(--c) 35%,transparent);opacity:0;transition:all .55s cubic-bezier(.22,1,.36,1);pointer-events:none}
    .box.on{opacity:1}
    .box.pick{border-style:dashed}
    .tag{position:absolute;left:-2px;top:-24px;background:var(--c);color:#fff;font-size:11px;font-weight:600;padding:3px 8px;border-radius:7px 7px 7px 0;white-space:nowrap;max-width:360px;overflow:hidden;text-overflow:ellipsis}
    .cur{position:fixed;left:0;top:0;transition:transform .8s cubic-bezier(.34,1.3,.64,1);pointer-events:none;opacity:0}
    .cur.on{opacity:1}
    .cur svg{display:block;filter:drop-shadow(0 2px 3px rgba(0,0,0,.35));animation:bob 2.4s ease-in-out infinite}
    .cur .nm{position:absolute;left:16px;top:18px;background:var(--c);color:#fff;font-size:11.5px;font-weight:600;padding:3px 9px;border-radius:999px;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.25)}
    @keyframes bob{50%{transform:translate(2px,2px)}}
    .bub{position:fixed;width:300px;background:#111114;color:#f5f5f5;border:1px solid #2a2a30;border-radius:14px;padding:10px;box-shadow:0 14px 40px rgba(0,0,0,.4);pointer-events:auto;opacity:0;transform:translateY(6px) scale(.97);transition:opacity .2s,transform .25s cubic-bezier(.34,1.56,.64,1);visibility:hidden}
    .bub.on{opacity:1;transform:none;visibility:visible}
    .bub .h{font-size:11.5px;color:#a1a1aa;margin:0 2px 7px;display:flex;gap:6px;align-items:center}
    .bub .h i{width:8px;height:8px;border-radius:50%;background:var(--c);display:inline-block}
    .bub textarea{width:100%;min-height:58px;resize:none;background:#1b1b20;color:#f5f5f5;border:1px solid #2a2a30;border-radius:10px;padding:8px 10px;font-size:13px;outline:none}
    .bub textarea:focus{border-color:var(--c)}
    .bub .row{display:flex;align-items:center;gap:8px;margin-top:8px}
    .bub .row span{font-size:11px;color:#71717a;flex:1}
    .bub button{background:var(--c);color:#fff;border:0;border-radius:9px;padding:6px 12px;font-size:12.5px;font-weight:600;cursor:pointer;transition:transform .2s cubic-bezier(.34,1.56,.64,1)}
    .bub button:active{transform:scale(.94)}
    .bub .ok{font-size:13px;padding:8px 4px;color:#4ade80;display:none}
    .bub.sent .ok{display:block}.bub.sent textarea,.bub.sent .row{display:none}
  </style>
  <div class="box"><div class="tag"></div></div>
  <div class="cur"><svg width="22" height="22" viewBox="0 0 24 24"><path d="M3 2l7.5 19 2.6-7.9L21 10.5z" fill="var(--c)" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/></svg><div class="nm"></div></div>
  <div class="bub"><div class="h"><i></i><span class="ttl"></span></div><textarea placeholder="e.g. soften the corners a bit more"></textarea><div class="row"><span>⌘/Ctrl+Enter to send · Alt+click anything to comment</span><button>Send</button></div><div class="ok">✓ sent — it'll see this on its next step</div></div>`;
  const $ = (s) => root.querySelector(s);
  const box = $(".box"), tag = $(".tag"), cur = $(".cur"), nm = $(".nm"), bub = $(".bub"), ta = $("textarea"), ttl = $(".ttl");
  let state = { agent: "AI", color: "#d95926", file: "", el: null, picked: null, hoverT: 0 };

  const mount = () => document.body && !host.isConnected && document.body.appendChild(host);
  mount();
  new MutationObserver(mount).observe(document.documentElement, { childList: true, subtree: false });

  function visible(el) {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && cs.visibility !== "hidden" && cs.display !== "none";
  }
  function bySelectors(list) {
    for (const s of list || []) {
      let nodes = [];
      try { nodes = document.querySelectorAll(s); } catch (e) { continue; }
      for (const n of nodes) if (!host.contains(n) && visible(n)) return n;
    }
    return null;
  }
  function byText(list) {
    for (const t of list || []) {
      const needle = t.toLowerCase().trim();
      if (needle.length < 3) continue;
      const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let n;
      while ((n = w.nextNode())) {
        if (n.nodeValue && n.nodeValue.toLowerCase().includes(needle)) {
          let el = n.parentElement;
          while (el && !visible(el)) el = el.parentElement;
          if (el && el !== document.body) return el;
        }
      }
    }
    return null;
  }
  function describe(el) {
    if (!el) return "";
    if (el.id) return "#" + el.id;
    const cls = [...el.classList].filter((c) => /^[\w-]+$/.test(c)).slice(0, 3);
    return el.tagName.toLowerCase() + (cls.length ? "." + cls.join(".") : "");
  }
  function paint() {
    const el = state.picked || state.el;
    if (el && el.isConnected) {
      const r = el.getBoundingClientRect();
      const pad = 4;
      Object.assign(box.style, { left: r.left - pad + "px", top: r.top - pad + "px", width: r.width + pad * 2 + "px", height: r.height + pad * 2 + "px" });
      box.classList.add("on");
      if (!state.picked) cur.style.transform = `translate(${Math.min(innerWidth - 120, r.right - 10)}px,${Math.min(innerHeight - 40, r.bottom - 10)}px)`;
    } else {
      box.classList.remove("on");
      cur.style.transform = `translate(${innerWidth - 200}px,${innerHeight - 70}px)`;
    }
    requestAnimationFrame(paint);
  }
  requestAnimationFrame(paint);

  function setColor(c) { host.style.setProperty("--c", c); root.host.style.setProperty("--c", c); }
  function openBubble(el, title) {
    const r = (el || box).getBoundingClientRect();
    let x = Math.min(innerWidth - 312, Math.max(8, r.left));
    let y = r.bottom + 12;
    if (y + 150 > innerHeight) y = Math.max(8, r.top - 160);
    Object.assign(bub.style, { left: x + "px", top: y + "px" });
    ttl.textContent = title;
    bub.classList.remove("sent");
    bub.classList.add("on");
  }
  function closeBubble() {
    if (document.activeElement === host && ta.value.trim()) return;
    bub.classList.remove("on");
    if (state.picked) { state.picked = null; box.classList.remove("pick"); }
  }

  window.addEventListener("message", (e) => {
    if (e.origin !== PARENT) return;
    const m = e.data || {};
    if (m.type !== "nyra-focus") return;
    state.agent = m.agent || state.agent;
    state.color = m.color || state.color;
    state.file = m.file || "";
    setColor(state.color);
    nm.textContent = state.agent + (m.action ? " · " + m.action : "");
    tag.textContent = state.agent + " is editing " + (m.file || "");
    cur.classList.add("on");
    const el = bySelectors(m.hints && m.hints.selectors) || byText(m.hints && m.hints.texts);
    const changed = el !== state.el;
    state.el = el;
    if (el && changed && m.scroll !== false) {
      const r = el.getBoundingClientRect();
      if (r.top < 0 || r.bottom > innerHeight) el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    parent.postMessage({ type: "nyra-found", found: !!el, selector: describe(el) }, PARENT);
  });

  document.addEventListener("mousemove", (e) => {
    const el = state.picked || state.el;
    if (!el || bub.classList.contains("on") && state.picked) return;
    const r = box.getBoundingClientRect();
    const inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
    const b = bub.getBoundingClientRect();
    const inB = bub.classList.contains("on") && e.clientX >= b.left - 10 && e.clientX <= b.right + 10 && e.clientY >= b.top - 14 && e.clientY <= b.bottom + 10;
    if (inside && !bub.classList.contains("on")) {
      clearTimeout(state.hoverT);
      state.hoverT = setTimeout(() => openBubble(el, `Tell ${state.agent} about this`), 280);
    } else if (!inside && !inB) {
      clearTimeout(state.hoverT);
      if (!ta.value.trim()) state.hoverT = setTimeout(closeBubble, 500);
    }
  }, true);

  document.addEventListener("click", (e) => {
    if (!e.altKey) return;
    if (host.contains(e.target)) return;
    e.preventDefault();
    e.stopPropagation();
    state.picked = e.target;
    box.classList.add("pick");
    openBubble(e.target, `Comment on ${describe(e.target)}`);
    setTimeout(() => ta.focus(), 50);
  }, true);

  function send() {
    const text = ta.value.trim();
    if (!text) return;
    const el = state.picked || state.el;
    const snippet = el ? (el.innerText || "").trim().replace(/\s+/g, " ").slice(0, 80) : "";
    parent.postMessage({ type: "nyra-feedback", text, target: { selector: describe(el), text: snippet, url: location.pathname } }, PARENT);
    ta.value = "";
    bub.classList.add("sent");
    setTimeout(closeBubble, 1600);
  }
  $("button").addEventListener("click", send);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send();
    if (e.key === "Escape") { ta.value = ""; closeBubble(); }
    e.stopPropagation();
  });
  parent.postMessage({ type: "nyra-ready" }, PARENT);
})();
