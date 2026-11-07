import asyncio, base64, json, os, sys, time
import websockets

BASE = sys.argv[1]
OUT = sys.argv[2]
MODE = sys.argv[3]
W, H = 1440, 900


class B:
    def __init__(self, ws):
        self.ws, self.n, self.pend = ws, 0, {}
        asyncio.get_event_loop().create_task(self.reader())

    async def reader(self):
        async for raw in self.ws:
            m = json.loads(raw)
            f = self.pend.pop(m.get("id"), None)
            if f and not f.done():
                f.set_result(m)

    async def cmd(self, method, params):
        self.n += 1
        i = self.n
        f = asyncio.get_event_loop().create_future()
        self.pend[i] = f
        await self.ws.send(json.dumps({"id": i, "method": method, "params": params}))
        m = await f
        if m.get("type") == "error":
            raise RuntimeError(f"{method}: {m.get('message')}")
        return m["result"]


async def main():
    async with websockets.connect("ws://127.0.0.1:9333/session", max_size=200_000_000, ping_interval=None) as ws:
        b = B(ws)
        await b.cmd("session.new", {"capabilities": {}})
        ctx = (await b.cmd("browsingContext.create", {"type": "tab"}))["context"]
        dpr = 2 if MODE == "stills" else 1
        await b.cmd("browsingContext.setViewport", {"context": ctx, "viewport": {"width": W, "height": H}, "devicePixelRatio": dpr})

        async def ev(expr, c=None):
            r = await b.cmd("script.evaluate", {"target": {"context": c or ctx}, "expression": expr, "awaitPromise": True})
            return (r.get("result") or {}).get("value")

        async def rect(sel, c=None):
            v = await ev(f"(()=>{{const e=document.querySelector({json.dumps(sel)});if(!e)return null;const r=e.getBoundingClientRect();return JSON.stringify([r.x,r.y,r.width,r.height])}})()", c)
            return json.loads(v) if v else None

        async def child():
            for _ in range(40):
                t = await b.cmd("browsingContext.getTree", {"root": ctx})
                k = t["contexts"][0].get("children") or []
                if k:
                    ok = await ev("!!document.querySelector('.pricing-card.featured') && [...document.body.children].some(e=>e.shadowRoot)", k[0]["context"])
                    if ok:
                        return k[0]["context"]
                await asyncio.sleep(0.25)
            raise RuntimeError("preview iframe never loaded")

        async def shot(name):
            r = await b.cmd("browsingContext.captureScreenshot", {"context": ctx})
            open(os.path.join(OUT, name), "wb").write(base64.b64decode(r["data"]))

        cur = [W - 200, H - 120]

        async def cursor_on():
            await ev("""(()=>{if(document.getElementById('__c'))return;const c=document.createElement('div');c.id='__c';
              c.innerHTML='<svg width="22" height="22" viewBox="0 0 24 24"><path d="M4 2l15 10.5-6.6 1.3L16.3 21l-3 1.4-3.9-7.3L4 19.6z" fill="#fff" stroke="#000" stroke-width="1.4" stroke-linejoin="round"/></svg>';
              c.style.cssText='position:fixed;left:0;top:0;z-index:2147483647;pointer-events:none;transition:transform .5s cubic-bezier(.22,1,.36,1);filter:drop-shadow(0 2px 3px rgba(0,0,0,.4))';
              document.documentElement.appendChild(c)})()""")
            await ev(f"document.getElementById('__c').style.transform='translate({cur[0]}px,{cur[1]}px)'")

        async def move(x, y, dur=600):
            cur[0], cur[1] = x, y
            await ev(f"(()=>{{const c=document.getElementById('__c');c.style.transitionDuration='{dur}ms';c.style.transform='translate({x}px,{y}px)'}})()")
            await b.cmd("input.performActions", {"context": ctx, "actions": [{"type": "pointer", "id": "m", "actions": [{"type": "pointerMove", "x": int(x), "y": int(y), "duration": dur}]}]})

        async def click(x=None, y=None):
            if x is not None:
                await move(x, y)
            await ev("(()=>{const c=document.getElementById('__c');c.animate([{transform:c.style.transform+' scale(1)'},{transform:c.style.transform+' scale(.8)'},{transform:c.style.transform+' scale(1)'}],{duration:220})})()")
            await b.cmd("input.performActions", {"context": ctx, "actions": [{"type": "pointer", "id": "m", "actions": [{"type": "pointerDown", "button": 0}, {"type": "pause", "duration": 60}, {"type": "pointerUp", "button": 0}]}]})

        async def click_sel(sel, dx=0.5, dy=0.5):
            r = await rect(sel)
            if not r:
                raise RuntimeError("missing " + sel)
            await click(r[0] + r[2] * dx, r[1] + r[3] * dy)

        async def typ(text, gap=45):
            acts = []
            for ch in text:
                acts += [{"type": "keyDown", "value": ch}, {"type": "keyUp", "value": ch}, {"type": "pause", "duration": gap}]
            await b.cmd("input.performActions", {"context": ctx, "actions": [{"type": "key", "id": "k", "actions": acts}]})

        async def keys(*vals):
            acts = [{"type": "keyDown", "value": v} for v in vals] + [{"type": "keyUp", "value": v} for v in reversed(vals)]
            await b.cmd("input.performActions", {"context": ctx, "actions": [{"type": "key", "id": "k", "actions": acts}]})

        nav = [0]

        async def go(h, wait=3.5):
            nav[0] += 1
            await b.cmd("browsingContext.navigate", {"context": ctx, "url": BASE + f"?n={nav[0]}#" + h, "wait": "complete"})
            await asyncio.sleep(wait)

        if MODE == "stills":
            await go("overview", 5)
            await shot("overview.png")
            await go("agent:claude", 3)
            await shot("agent.png")
            await go("watch:claude:" + os.environ["WEB"], 6)
            fr = await rect("#pvf")
            c2 = await child()
            r = json.loads(await ev("(()=>{const r=document.querySelector('.pricing-card.featured').getBoundingClientRect();return JSON.stringify([r.x,r.y,r.width,r.height])})()", c2))
            await b.cmd("input.performActions", {"context": ctx, "actions": [{"type": "pointer", "id": "m", "actions": [{"type": "pointerMove", "x": int(fr[0] + r[0] + r[2] * .5), "y": int(fr[1] + r[1] + r[3] * .4), "duration": 300}]}]})
            await asyncio.sleep(1.5)
            await shot("watch.png")
            await go("run:runcla01", 3)
            await shot("prompt-compare.png")
            await go("prompt", 2.5)
            await shot("prompt.png")
            await go("mcp", 3)
            await shot("mcp.png")
            await go("usage", 3)
            await shot("usage.png")
            await go("doc:" + os.environ["DOC"], 3.5)
            await shot("collab.png")
            await go("local", 3)
            await shot("local-ai.png")
            return

        frames = []

        async def snap():
            r = await b.cmd("browsingContext.captureScreenshot", {"context": ctx})
            frames.append((time.time(), r["data"]))

        async def wait(sec):
            end = time.time() + sec
            while time.time() < end:
                await snap()

        async def move(x, y, dur=600):
            sx, sy = cur
            n = max(1, int(dur / 110))
            for i in range(1, n + 1):
                t = i / n
                e = 1 - (1 - t) ** 3
                px, py = sx + (x - sx) * e, sy + (y - sy) * e
                await ev(f"(()=>{{const c=document.getElementById('__c');c.style.transitionDuration='0ms';c.style.transform='translate({px}px,{py}px)'}})()")
                await b.cmd("input.performActions", {"context": ctx, "actions": [{"type": "pointer", "id": "m", "actions": [{"type": "pointerMove", "x": int(px), "y": int(py), "duration": 0}]}]})
                await snap()
            cur[0], cur[1] = x, y

        async def click(x=None, y=None):
            if x is not None:
                await move(x, y)
            await b.cmd("input.performActions", {"context": ctx, "actions": [{"type": "pointer", "id": "m", "actions": [{"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}]}]})
            await snap()

        async def click_sel(sel, dx=0.5, dy=0.5):
            r = await rect(sel)
            if not r:
                raise RuntimeError("missing " + sel)
            await click(r[0] + r[2] * dx, r[1] + r[3] * dy)

        async def typ(text, gap=45):
            for i in range(0, len(text), 2):
                acts = []
                for ch in text[i:i + 2]:
                    acts += [{"type": "keyDown", "value": ch}, {"type": "keyUp", "value": ch}]
                await b.cmd("input.performActions", {"context": ctx, "actions": [{"type": "key", "id": "k", "actions": acts}]})
                await snap()

        await go("overview", 2)
        await ev("localStorage.clear()")
        await go("overview", 5)
        await cursor_on()
        await wait(3.5)
        await move(700, 330, 900)
        await wait(0.8)
        await click_sel(".bm-i[data-v='agent:claude']")
        await wait(2.5)
        await click_sel("button[data-act='watch'][data-cwd$='acme-web']")
        await wait(4.5)
        fr = await rect("#pvf")
        c2 = await child()
        r = json.loads(await ev("(()=>{const h=[...document.body.children].find(e=>e.shadowRoot);const r=h.shadowRoot.querySelector('.box').getBoundingClientRect();return JSON.stringify([r.x,r.y,r.width,r.height])})()", c2))
        await move(fr[0] + r[0] + r[2] * .5, fr[1] + r[1] + r[3] * .35, 900)
        await wait(1.4)
        tr = json.loads(await ev("(()=>{const h=[...document.body.children].find(e=>e.shadowRoot);const r=h.shadowRoot.querySelector('textarea').getBoundingClientRect();return JSON.stringify([r.x,r.y,r.width,r.height])})()", c2))
        await click(fr[0] + tr[0] + 40, fr[1] + tr[1] + 20)
        await typ("soften the corners a bit more bro")
        await wait(0.5)
        br = json.loads(await ev("(()=>{const h=[...document.body.children].find(e=>e.shadowRoot);const r=h.shadowRoot.querySelector('button').getBoundingClientRect();return JSON.stringify([r.x,r.y,r.width,r.height])})()", c2))
        await click(fr[0] + br[0] + br[2] / 2, fr[1] + br[1] + br[3] / 2)
        await wait(2.0)
        await click_sel(".brand", 0.5, 0.5)
        await keys("\ue009", "k")
        await wait(1.0)
        await typ("are the corners on the pricing cards too sharp?", 35)
        await click_sel("#pag button[data-k='codex']")
        await wait(0.4)
        await click_sel("#pcwd", 0.3, 0.5)
        await keys("\ue009", "a")
        await keys("\ue003")
        await typ("~/code/acme-web", 30)
        await click_sel("button[data-act='prun']")
        await wait(9.0)
        await click_sel(".bm-i[data-v='mcp']")
        await wait(3.0)
        await click_sel(".bm-i[data-v='overview']")
        await wait(2.5)
        t0 = frames[0][0]
        lst = []
        for i, (t, data) in enumerate(frames):
            fn = os.path.join(OUT, f"f{i:05d}.png")
            open(fn, "wb").write(base64.b64decode(data))
            nxt = frames[i + 1][0] if i + 1 < len(frames) else t + 0.5
            lst.append(f"file '{fn}'\nduration {max(0.02, nxt - t):.3f}\n")
        lst.append(f"file '{os.path.join(OUT, f'f{len(frames) - 1:05d}.png')}'\n")
        open(os.path.join(OUT, "frames.txt"), "w").write("".join(lst))
        print(f"{len(frames)} frames over {frames[-1][0] - t0:.1f}s")


asyncio.run(main())

