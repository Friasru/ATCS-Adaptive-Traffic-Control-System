"""Shared helpers for driving the 3D page with the Chromium that Playwright already caches on this machine."""
import glob
import os

from playwright.async_api import async_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Project-local install first (./run.sh install-browser), then the user's shared Playwright cache.
_CANDIDATES = [os.path.join(ROOT, ".cache", "ms-playwright"), os.path.expanduser("~/.cache/ms-playwright")]
CHROME = next(
    (p for base in _CANDIDATES for p in sorted(glob.glob(os.path.join(base, "chromium-*", "chrome-linux*", "chrome")), reverse=True)),
    None,
)
# Software GL (SwiftShader) so WebGL works headless under WSL. FPS measured this way is NOT the Windows-GPU number.
ARGS = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]


# Real GPU through WSL's Mesa D3D12 layer (shows up as the NVIDIA GPU). Used for performance measurements only.
GPU_ARGS = ["--use-gl=angle", "--use-angle=gl-egl", "--ignore-gpu-blocklist", "--enable-gpu", "--disable-gpu-sandbox"]
GPU_ENV = {"GALLIUM_DRIVER": "d3d12", "MESA_D3D12_DEFAULT_ADAPTER_NAME": "NVIDIA", "LD_LIBRARY_PATH": "/usr/lib/wsl/lib"}


class Browser:
    def __init__(self, width=1600, height=900, gpu=False, video_dir=None):
        self.width, self.height, self.gpu, self.video_dir = width, height, gpu, video_dir

    async def __aenter__(self):
        if CHROME is None:
            raise RuntimeError("No Chromium found for the browser tests. Install one with:  ./run.sh install-browser")
        self._pw = await async_playwright().start()
        if self.gpu:
            self.browser = await self._pw.chromium.launch(executable_path=CHROME, args=GPU_ARGS, headless=True,
                                                          env={**os.environ, **GPU_ENV})
        else:
            self.browser = await self._pw.chromium.launch(executable_path=CHROME, args=ARGS, headless=True)
        opts = {"viewport": {"width": self.width, "height": self.height}}
        if self.video_dir:
            opts.update(record_video_dir=self.video_dir, record_video_size={"width": self.width, "height": self.height})
        self.context = await self.browser.new_context(**opts)
        self.page = await self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        self.page.on("console", lambda m: self.errors.append(f"console.{m.type}: {m.text}")
                     if m.type == "error" and "favicon" not in m.text and "404" not in m.text else None)
        return self

    async def __aexit__(self, *exc):
        await self.context.close()  # finalises a recorded video
        await self.browser.close()
        await self._pw.stop()

    async def gl_renderer(self):
        return await self.page.evaluate("""() => { const gl = document.createElement('canvas').getContext('webgl2');
            const e = gl.getExtension('WEBGL_debug_renderer_info'); return gl.getParameter(e.UNMASKED_RENDERER_WEBGL); }""")

    async def open(self, base, query=""):
        pg = self.page
        await pg.goto(f"{base}/{query}")
        await pg.wait_for_function("window.__atcs && document.querySelector('#sel-ctrl').options.length > 0", timeout=20000)
        await self.settle_idle()
        return pg

    async def settle_idle(self):
        """Stop whatever run a previous page left on the server and wait until the UI says nothing is active."""
        pg = self.page
        await pg.wait_for_function("window.__atcs.status !== undefined")
        if await pg.is_enabled("#btn-stop"):
            await pg.click("#btn-stop")
        await pg.wait_for_function("!document.querySelector('#btn-start').disabled", timeout=20000)

    async def start_run(self, controllers=("fixed",), scenario=None, speed=10):
        """Select, start, and wait until *this* run is live (not frames left over from an earlier one)."""
        pg = self.page
        await self.settle_idle()
        if scenario:
            await pg.select_option("#sel-scenario", scenario)
        compare = len(controllers) == 2
        pressed = await pg.get_attribute("#btn-mode", "aria-pressed") == "true"
        if compare != pressed:
            await pg.click("#btn-mode")
        await pg.select_option("#sel-ctrl", controllers[0])
        if compare:
            await pg.select_option("#sel-ctrl2", controllers[1])
        await pg.click(f"#speeds button[data-speed='{speed}']")
        await pg.wait_for_function("!document.querySelector('#btn-start').disabled")
        await pg.click("#btn-start")
        await pg.wait_for_function(
            "window.__atcs.status === 'running' && window.__atcs.lastFrame && window.__atcs.lastFrame.run_id === window.__atcs.runId",
            timeout=30000)

    async def wait_sim_time(self, t, timeout=180000):
        await self.page.wait_for_function(
            f"window.__atcs.lastFrame && window.__atcs.lastFrame.run_id === window.__atcs.runId && window.__atcs.lastFrame.t >= {t}",
            timeout=timeout)
