// D-Hub Overlay: Proxy-dxgi.dll fuer 64-Bit-DirectX-11-Spiele (Einzelspieler).
// Zeichnet Fadenkreuz, FPS und Textzeilen, die D-Hub in dhub_overlay.cfg schreibt. Entfernen = diese dxgi.dll loeschen.
// Build: cl /LD /O2 /EHsc /std:c++17 dhub_overlay.cpp /Fe:dhub_overlay.dll /link user32.lib gdi32.lib
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <algorithm>
#include <string>
#include <vector>
#include <cstdio>
#include <cstdlib>
#include <cstring>

extern "C" __declspec(dllexport) const char DHUBOVERLAYv1[] = "DHUBOVERLAYv1";  // Erkennungsmarke fuer D-Hub

// ---------- Weiterleitung an die echte dxgi.dll ----------
static HMODULE g_real = nullptr;
static FARPROC RealFn(const char* n) {
    if (!g_real) {
        wchar_t p[MAX_PATH];
        GetSystemDirectoryW(p, MAX_PATH);
        wcscat_s(p, L"\\dxgi.dll");
        g_real = LoadLibraryW(p);
    }
    return g_real ? GetProcAddress(g_real, n) : nullptr;
}
extern "C" {
HRESULT WINAPI Proxy_CreateDXGIFactory(REFIID r, void** o) { auto f = (HRESULT(WINAPI*)(REFIID, void**))RealFn("CreateDXGIFactory"); return f ? f(r, o) : E_FAIL; }
HRESULT WINAPI Proxy_CreateDXGIFactory1(REFIID r, void** o) { auto f = (HRESULT(WINAPI*)(REFIID, void**))RealFn("CreateDXGIFactory1"); return f ? f(r, o) : E_FAIL; }
HRESULT WINAPI Proxy_CreateDXGIFactory2(UINT fl, REFIID r, void** o) { auto f = (HRESULT(WINAPI*)(UINT, REFIID, void**))RealFn("CreateDXGIFactory2"); return f ? f(fl, r, o) : E_FAIL; }
HRESULT WINAPI Proxy_DXGIGetDebugInterface1(UINT fl, REFIID r, void** o) { auto f = (HRESULT(WINAPI*)(UINT, REFIID, void**))RealFn("DXGIGetDebugInterface1"); return f ? f(fl, r, o) : E_FAIL; }
HRESULT WINAPI Proxy_DXGIDeclareAdapterRemovalSupport() { auto f = (HRESULT(WINAPI*)())RealFn("DXGIDeclareAdapterRemovalSupport"); return f ? f() : S_OK; }
}
#pragma comment(linker, "/export:CreateDXGIFactory=Proxy_CreateDXGIFactory")
#pragma comment(linker, "/export:CreateDXGIFactory1=Proxy_CreateDXGIFactory1")
#pragma comment(linker, "/export:CreateDXGIFactory2=Proxy_CreateDXGIFactory2")
#pragma comment(linker, "/export:DXGIGetDebugInterface1=Proxy_DXGIGetDebugInterface1")
#pragma comment(linker, "/export:DXGIDeclareAdapterRemovalSupport=Proxy_DXGIDeclareAdapterRemovalSupport")

// ---------- Einstellungen aus dhub_overlay.cfg ----------
struct Cfg {
    bool visible = true, cross = false, fps = false;
    int cstyle = 0, csize = 12, cgap = 4, cthick = 2;
    COLORREF ccolor = RGB(0x5c, 0xff, 0x0c);
    std::string spos = "tr";
    std::vector<std::wstring> lines, kf;  // kf: Eintrag mit fuehrendem '*' = spricht gerade
};
static Cfg g_cfg;
static CRITICAL_SECTION g_cs;
static std::wstring g_dir;
static FILETIME g_cfgTime = {};
static ULONGLONG g_cfgCheck = 0;
static bool g_dirty = true;

static std::wstring Wide(const std::string& s) {
    if (s.empty()) return L"";
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring w(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), &w[0], n);
    return w;
}
static std::vector<std::wstring> SplitPipe(const std::string& s) {
    std::vector<std::wstring> out;
    size_t a = 0;
    while (a <= s.size()) {
        size_t b = s.find('|', a);
        if (b == std::string::npos) b = s.size();
        if (b > a) out.push_back(Wide(s.substr(a, b - a)));
        a = b + 1;
    }
    return out;
}
static void LoadCfg() {
    std::wstring p = g_dir + L"dhub_overlay.cfg";
    WIN32_FILE_ATTRIBUTE_DATA fa;
    if (!GetFileAttributesExW(p.c_str(), GetFileExInfoStandard, &fa)) return;
    if (CompareFileTime(&fa.ftLastWriteTime, &g_cfgTime) == 0) return;
    FILE* f = _wfopen(p.c_str(), L"rb");
    if (!f) return;
    Cfg c;
    char buf[2048];
    while (fgets(buf, sizeof buf, f)) {
        std::string l(buf);
        while (!l.empty() && (l.back() == '\n' || l.back() == '\r')) l.pop_back();
        size_t e = l.find('=');
        if (e == std::string::npos) continue;
        std::string k = l.substr(0, e), v = l.substr(e + 1);
        int iv = atoi(v.c_str());
        if (k == "visible") c.visible = iv != 0;
        else if (k == "cross") c.cross = iv != 0;
        else if (k == "fps") c.fps = iv != 0;
        else if (k == "cstyle") c.cstyle = v == "plus" ? 1 : v == "dot" ? 2 : v == "circle" ? 3 : 0;
        else if (k == "csize") c.csize = iv;
        else if (k == "cgap") c.cgap = iv;
        else if (k == "cthick") c.cthick = iv < 1 ? 1 : iv;
        else if (k == "ccolor" && v.size() >= 6) { unsigned x = (unsigned)strtoul(v.c_str() + (v[0] == '#' ? 1 : 0), nullptr, 16); c.ccolor = RGB((x >> 16) & 255, (x >> 8) & 255, x & 255); }
        else if (k == "spos") c.spos = v;
        else if (k == "lines") c.lines = SplitPipe(v);
        else if (k == "kf") c.kf = SplitPipe(v);
    }
    fclose(f);
    EnterCriticalSection(&g_cs);
    g_cfg = c;
    g_cfgTime = fa.ftLastWriteTime;
    g_dirty = true;
    LeaveCriticalSection(&g_cs);
}

// ---------- Overlay-Bild per GDI ----------
struct Box { int x1, y1, x2, y2; };
static HDC g_mdc = nullptr;
static HBITMAP g_bmp = nullptr;
static unsigned* g_pix = nullptr;
static int g_bw = 0, g_bh = 0;
static std::vector<Box> g_prev;
static int g_fps = 0;

static void FreeBitmap() {
    if (g_bmp) { DeleteObject(g_bmp); g_bmp = nullptr; }
    if (g_mdc) { DeleteDC(g_mdc); g_mdc = nullptr; }
    g_pix = nullptr;
}
static bool EnsureBitmap(int w, int h) {
    if (g_bmp && w == g_bw && h == g_bh) return true;
    FreeBitmap();
    BITMAPINFO bi = {};
    bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bi.bmiHeader.biWidth = w;
    bi.bmiHeader.biHeight = -h;
    bi.bmiHeader.biPlanes = 1;
    bi.bmiHeader.biBitCount = 32;
    bi.bmiHeader.biCompression = BI_RGB;
    HDC sd = GetDC(nullptr);
    g_mdc = CreateCompatibleDC(sd);
    g_bmp = CreateDIBSection(sd, &bi, DIB_RGB_COLORS, (void**)&g_pix, nullptr, 0);
    ReleaseDC(nullptr, sd);
    if (!g_bmp || !g_pix) { FreeBitmap(); return false; }
    SelectObject(g_mdc, g_bmp);
    SetBkMode(g_mdc, TRANSPARENT);
    memset(g_pix, 0, (size_t)w * h * 4);
    g_bw = w; g_bh = h;
    g_prev.clear();
    return true;
}
static HFONT Font(int h, bool bold) {
    return CreateFontW(-h, 0, 0, 0, bold ? 700 : 500, 0, 0, 0, DEFAULT_CHARSET, 0, 0, NONANTIALIASED_QUALITY, 0, L"Segoe UI");
}
static void Fill(std::vector<Box>& cur, int x1, int y1, int x2, int y2, COLORREF c) {
    RECT r = { x1, y1, x2, y2 };
    HBRUSH b = CreateSolidBrush(c);
    FillRect(g_mdc, &r, b);
    DeleteObject(b);
    cur.push_back({ x1, y1, x2, y2 });
}
static void Panel(std::vector<Box>& cur, int x, int y, const std::vector<std::wstring>& lines, int firstBig, bool kfMode, const std::vector<bool>& hot) {
    if (lines.empty()) return;
    int lh = 24, w = 170;
    HFONT f = Font(16, false), fb = Font(22, true);
    for (size_t i = 0; i < lines.size(); i++) {
        SelectObject(g_mdc, (int)i == firstBig ? fb : f);
        SIZE sz;
        GetTextExtentPoint32W(g_mdc, lines[i].c_str(), (int)lines[i].size(), &sz);
        if (sz.cx + 24 > w) w = sz.cx + 24;
    }
    int h = (int)lines.size() * lh + 12;
    // 0x010101 ist die Markierung fuer das halbtransparente Schwarz des Hintergrunds
    Fill(cur, x, y, x + w, y + h, RGB(1, 1, 1));
    for (size_t i = 0; i < lines.size(); i++) {
        bool big = (int)i == firstBig;
        SelectObject(g_mdc, big ? fb : f);
        SetTextColor(g_mdc, (big || (i < hot.size() && hot[i])) ? RGB(0x5c, 0xff, 0x0c) : RGB(0xee, 0xee, 0xee));
        RECT r = { x + 12, y + 6 + (int)i * lh, x + w, y + 6 + (int)(i + 1) * lh };
        DrawTextW(g_mdc, lines[i].c_str(), -1, &r, DT_SINGLELINE | DT_NOPREFIX | DT_VCENTER);
    }
    DeleteObject(f);
    DeleteObject(fb);
}
// Zeichnet das Overlay neu. Liefert die zu aktualisierenden Bereiche (alte und neue).
static void Compose(int w, int h, std::vector<Box>& cur) {
    for (auto& b : g_prev)  // alte Bereiche leeren
        for (int y = std::max(0, b.y1); y < std::min(h, b.y2); y++)
            memset(g_pix + (size_t)y * w + std::max(0, b.x1), 0, (size_t)(std::min(w, b.x2) - std::max(0, b.x1)) * 4);
    Cfg c;
    EnterCriticalSection(&g_cs);
    c = g_cfg;
    LeaveCriticalSection(&g_cs);
    if (!c.visible) return;
    if (c.cross) {
        int cx = w / 2, cy = h / 2, s = c.csize, g = c.cgap, t = c.cthick, hh = t / 2;
        if (c.cstyle == 0 || c.cstyle == 1) {
            int gp = c.cstyle == 0 ? g : 0;
            Fill(cur, cx - gp - s, cy - hh, cx - gp, cy - hh + t, c.ccolor);
            Fill(cur, cx + gp, cy - hh, cx + gp + s, cy - hh + t, c.ccolor);
            Fill(cur, cx - hh, cy - gp - s, cx - hh + t, cy - gp, c.ccolor);
            Fill(cur, cx - hh, cy + gp, cx - hh + t, cy + gp + s, c.ccolor);
            if (c.cstyle == 1) Fill(cur, cx - hh, cy - hh, cx - hh + t, cy - hh + t, c.ccolor);
        } else if (c.cstyle == 2) {
            Fill(cur, cx - t - 1, cy - t - 1, cx + t + 2, cy + t + 2, c.ccolor);
        } else {
            HPEN p = CreatePen(PS_SOLID, t, c.ccolor);
            HGDIOBJ op = SelectObject(g_mdc, p), ob = SelectObject(g_mdc, GetStockObject(NULL_BRUSH));
            Ellipse(g_mdc, cx - s, cy - s, cx + s, cy + s);
            SelectObject(g_mdc, ob); SelectObject(g_mdc, op); DeleteObject(p);
            cur.push_back({ cx - s - t, cy - s - t, cx + s + t + 1, cy + s + t + 1 });
            Fill(cur, cx - 1, cy - 1, cx + 2, cy + 2, c.ccolor);
        }
    }
    int ky = 14;
    if (!c.kf.empty()) {
        std::vector<std::wstring> l;
        std::vector<bool> hot;
        for (auto& e : c.kf) { bool sp = !e.empty() && e[0] == L'*'; l.push_back(sp ? e.substr(1) : e); hot.push_back(sp); }
        Panel(cur, 14, ky, l, -1, true, hot);
        ky += (int)l.size() * 24 + 20;
    }
    std::vector<std::wstring> lines;
    if (c.fps) { wchar_t b[32]; swprintf_s(b, L"FPS %d", g_fps); lines.push_back(b); }
    for (auto& l : c.lines) lines.push_back(l);
    if (!lines.empty()) {
        int bh = (int)lines.size() * 24 + 12;
        bool left = c.spos == "tl" || c.spos == "bl", bottom = c.spos == "bl" || c.spos == "br";
        int x = left ? 14 : w - 190, y = bottom ? h - bh - 14 : (left && !c.kf.empty() ? ky : 14);
        Panel(cur, x, y, lines, c.fps ? 0 : -1, false, {});
    }
}
static void Convert(int w, int h, const Box& b) {
    for (int y = std::max(0, b.y1); y < std::min(h, b.y2); y++)
        for (int x = std::max(0, b.x1); x < std::min(w, b.x2); x++) {
            unsigned& p = g_pix[(size_t)y * w + x];
            unsigned v = p & 0x00FFFFFF;
            p = v == 0 ? 0 : v == 0x010101 ? 0xA0000000u : (v | 0xFF000000u);
        }
}

// ---------- Direct3D 11 ----------
typedef HRESULT(WINAPI* PD3DCompile)(LPCVOID, SIZE_T, LPCSTR, const D3D_SHADER_MACRO*, ID3DInclude*, LPCSTR, LPCSTR, UINT, UINT, ID3DBlob**, ID3DBlob**);
static const char* kHlsl =
    "Texture2D t : register(t0); SamplerState s : register(s0);\n"
    "struct V { float4 p : SV_Position; float2 u : TEXCOORD0; };\n"
    "V vs(uint i : SV_VertexID) { V o; float2 uv = float2((i << 1) & 2, i & 2); o.u = uv; o.p = float4(uv * float2(2, -2) + float2(-1, 1), 0, 1); return o; }\n"
    "float4 ps(V i) : SV_Target { return t.Sample(s, i.u); }\n";
struct Res {
    ID3D11Device* dev = nullptr;
    ID3D11VertexShader* vs = nullptr; ID3D11PixelShader* ps = nullptr;
    ID3D11BlendState* bs = nullptr; ID3D11RasterizerState* rs = nullptr; ID3D11DepthStencilState* ds = nullptr; ID3D11SamplerState* ss = nullptr;
    ID3D11Texture2D* tex = nullptr; ID3D11ShaderResourceView* srv = nullptr; UINT tw = 0, th = 0;
    bool bad = false;
};
static Res R;
template <class T> static void Rel(T*& p) { if (p) { p->Release(); p = nullptr; } }
static void FreeRes() {
    Rel(R.vs); Rel(R.ps); Rel(R.bs); Rel(R.rs); Rel(R.ds); Rel(R.ss); Rel(R.srv); Rel(R.tex);
    R.dev = nullptr; R.tw = R.th = 0; R.bad = false;
}
static bool InitRes(ID3D11Device* dev) {
    FreeRes();
    R.dev = dev;
    HMODULE m = LoadLibraryW(L"d3dcompiler_47.dll");
    PD3DCompile comp = m ? (PD3DCompile)GetProcAddress(m, "D3DCompile") : nullptr;
    if (!comp) { R.bad = true; return false; }
    ID3DBlob *vb = nullptr, *pb = nullptr, *er = nullptr;
    if (FAILED(comp(kHlsl, strlen(kHlsl), nullptr, nullptr, nullptr, "vs", "vs_4_0", 0, 0, &vb, &er)) || FAILED(comp(kHlsl, strlen(kHlsl), nullptr, nullptr, nullptr, "ps", "ps_4_0", 0, 0, &pb, &er))) {
        Rel(vb); Rel(pb); Rel(er); R.bad = true; return false;
    }
    bool ok = SUCCEEDED(dev->CreateVertexShader(vb->GetBufferPointer(), vb->GetBufferSize(), nullptr, &R.vs)) &&
              SUCCEEDED(dev->CreatePixelShader(pb->GetBufferPointer(), pb->GetBufferSize(), nullptr, &R.ps));
    Rel(vb); Rel(pb); Rel(er);
    D3D11_BLEND_DESC bd = {};
    bd.RenderTarget[0].BlendEnable = TRUE;
    bd.RenderTarget[0].SrcBlend = D3D11_BLEND_ONE;
    bd.RenderTarget[0].DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
    bd.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
    bd.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ONE;
    bd.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_INV_SRC_ALPHA;
    bd.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
    bd.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    D3D11_RASTERIZER_DESC rd = {};
    rd.FillMode = D3D11_FILL_SOLID; rd.CullMode = D3D11_CULL_NONE; rd.DepthClipEnable = TRUE;
    D3D11_DEPTH_STENCIL_DESC dd = {};
    D3D11_SAMPLER_DESC sd = {};
    sd.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT; sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sd.MaxLOD = D3D11_FLOAT32_MAX;
    ok = ok && SUCCEEDED(dev->CreateBlendState(&bd, &R.bs)) && SUCCEEDED(dev->CreateRasterizerState(&rd, &R.rs)) &&
         SUCCEEDED(dev->CreateDepthStencilState(&dd, &R.ds)) && SUCCEEDED(dev->CreateSamplerState(&sd, &R.ss));
    if (!ok) { FreeRes(); R.dev = dev; R.bad = true; }
    return ok;
}
static bool EnsureTex(ID3D11Device* dev, UINT w, UINT h) {
    if (R.tex && R.tw == w && R.th == h) return true;
    Rel(R.srv); Rel(R.tex);
    D3D11_TEXTURE2D_DESC td = {};
    td.Width = w; td.Height = h; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    if (FAILED(dev->CreateTexture2D(&td, nullptr, &R.tex))) return false;
    if (FAILED(dev->CreateShaderResourceView(R.tex, nullptr, &R.srv))) { Rel(R.tex); return false; }
    R.tw = w; R.th = h;
    g_dirty = true;
    return true;
}
static DXGI_FORMAT RtvFormat(DXGI_FORMAT f) {
    switch (f) {
        case DXGI_FORMAT_R8G8B8A8_TYPELESS: return DXGI_FORMAT_R8G8B8A8_UNORM;
        case DXGI_FORMAT_B8G8R8A8_TYPELESS: return DXGI_FORMAT_B8G8R8A8_UNORM;
        case DXGI_FORMAT_R8G8B8A8_UNORM: case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB: case DXGI_FORMAT_B8G8R8A8_UNORM: case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB:
        case DXGI_FORMAT_R10G10B10A2_UNORM: return f;
        default: return DXGI_FORMAT_UNKNOWN;  // z. B. HDR: nichts zeichnen
    }
}
static void Draw(IDXGISwapChain* sc, ID3D11Device* dev) {
    if (R.dev != dev && !InitRes(dev)) return;
    if (R.bad) return;
    ID3D11DeviceContext* ctx = nullptr;
    dev->GetImmediateContext(&ctx);
    if (!ctx) return;
    ID3D11Texture2D* bb = nullptr;
    if (SUCCEEDED(sc->GetBuffer(0, __uuidof(ID3D11Texture2D), (void**)&bb)) && bb) {
        D3D11_TEXTURE2D_DESC bd;
        bb->GetDesc(&bd);
        DXGI_FORMAT rf = RtvFormat(bd.Format);
        if (rf != DXGI_FORMAT_UNKNOWN && bd.SampleDesc.Count == 1 && EnsureBitmap(bd.Width, bd.Height) && EnsureTex(dev, bd.Width, bd.Height)) {
            if (g_dirty) {
                g_dirty = false;
                std::vector<Box> cur;
                Compose(bd.Width, bd.Height, cur);
                for (auto& b : cur) Convert(bd.Width, bd.Height, b);
                std::vector<Box> all = g_prev;
                all.insert(all.end(), cur.begin(), cur.end());
                for (auto& b : all) {
                    int x1 = std::max(0, b.x1), y1 = std::max(0, b.y1), x2 = std::min((int)bd.Width, b.x2), y2 = std::min((int)bd.Height, b.y2);
                    if (x2 <= x1 || y2 <= y1) continue;
                    D3D11_BOX box = { (UINT)x1, (UINT)y1, 0, (UINT)x2, (UINT)y2, 1 };
                    ctx->UpdateSubresource(R.tex, 0, &box, g_pix + (size_t)y1 * bd.Width + x1, bd.Width * 4, 0);
                }
                g_prev = cur;
            }
            D3D11_RENDER_TARGET_VIEW_DESC rvd = {};
            rvd.Format = rf; rvd.ViewDimension = D3D11_RTV_DIMENSION_TEXTURE2D;
            ID3D11RenderTargetView* rtv = nullptr;
            if (SUCCEEDED(dev->CreateRenderTargetView(bb, &rvd, &rtv)) && rtv) {
                // Zustand des Spiels sichern
                ID3D11RenderTargetView* orv[D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT] = {}; ID3D11DepthStencilView* odsv = nullptr;
                ctx->OMGetRenderTargets(D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT, orv, &odsv);
                ID3D11BlendState* obs = nullptr; FLOAT bf[4]; UINT sm = 0; ctx->OMGetBlendState(&obs, bf, &sm);
                ID3D11DepthStencilState* ods = nullptr; UINT sref = 0; ctx->OMGetDepthStencilState(&ods, &sref);
                ID3D11RasterizerState* ors = nullptr; ctx->RSGetState(&ors);
                UINT nvp = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE; D3D11_VIEWPORT ovp[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE]; ctx->RSGetViewports(&nvp, ovp);
                UINT nsc = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE; D3D11_RECT osc[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE]; ctx->RSGetScissorRects(&nsc, osc);
                ID3D11InputLayout* oil = nullptr; ctx->IAGetInputLayout(&oil);
                D3D11_PRIMITIVE_TOPOLOGY otp; ctx->IAGetPrimitiveTopology(&otp);
                ID3D11VertexShader* ovs = nullptr; ctx->VSGetShader(&ovs, nullptr, nullptr);
                ID3D11PixelShader* ops = nullptr; ctx->PSGetShader(&ops, nullptr, nullptr);
                ID3D11GeometryShader* ogs = nullptr; ctx->GSGetShader(&ogs, nullptr, nullptr);
                ID3D11HullShader* ohs = nullptr; ctx->HSGetShader(&ohs, nullptr, nullptr);
                ID3D11DomainShader* ods2 = nullptr; ctx->DSGetShader(&ods2, nullptr, nullptr);
                ID3D11ShaderResourceView* osrv = nullptr; ctx->PSGetShaderResources(0, 1, &osrv);
                ID3D11SamplerState* oss = nullptr; ctx->PSGetSamplers(0, 1, &oss);
                // Overlay zeichnen
                D3D11_VIEWPORT vp = { 0, 0, (FLOAT)bd.Width, (FLOAT)bd.Height, 0, 1 };
                FLOAT nb[4] = {};
                ctx->OMSetRenderTargets(1, &rtv, nullptr);
                ctx->OMSetBlendState(R.bs, nb, 0xffffffff);
                ctx->OMSetDepthStencilState(R.ds, 0);
                ctx->RSSetState(R.rs);
                ctx->RSSetViewports(1, &vp);
                ctx->IASetInputLayout(nullptr);
                ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
                ctx->VSSetShader(R.vs, nullptr, 0);
                ctx->GSSetShader(nullptr, nullptr, 0);
                ctx->HSSetShader(nullptr, nullptr, 0);
                ctx->DSSetShader(nullptr, nullptr, 0);
                ctx->PSSetShader(R.ps, nullptr, 0);
                ctx->PSSetShaderResources(0, 1, &R.srv);
                ctx->PSSetSamplers(0, 1, &R.ss);
                ctx->Draw(3, 0);
                // Zustand zuruecksetzen
                ctx->OMSetRenderTargets(D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT, orv, odsv);
                ctx->OMSetBlendState(obs, bf, sm);
                ctx->OMSetDepthStencilState(ods, sref);
                ctx->RSSetState(ors);
                ctx->RSSetViewports(nvp, ovp);
                ctx->RSSetScissorRects(nsc, osc);
                ctx->IASetInputLayout(oil);
                ctx->IASetPrimitiveTopology(otp);
                ctx->VSSetShader(ovs, nullptr, 0);
                ctx->PSSetShader(ops, nullptr, 0);
                ctx->GSSetShader(ogs, nullptr, 0);
                ctx->HSSetShader(ohs, nullptr, 0);
                ctx->DSSetShader(ods2, nullptr, 0);
                ctx->PSSetShaderResources(0, 1, &osrv);
                ctx->PSSetSamplers(0, 1, &oss);
                for (auto& r : orv) Rel(r);
                Rel(odsv); Rel(obs); Rel(ods); Rel(ors); Rel(oil); Rel(ovs); Rel(ops); Rel(ogs); Rel(ohs); Rel(ods2); Rel(osrv); Rel(oss);
                rtv->Release();
            }
        }
        bb->Release();
    }
    ctx->Release();
}

// ---------- Hook auf Present ----------
typedef HRESULT(STDMETHODCALLTYPE* PresentFn)(IDXGISwapChain*, UINT, UINT);
typedef HRESULT(STDMETHODCALLTYPE* Present1Fn)(IDXGISwapChain1*, UINT, UINT, const DXGI_PRESENT_PARAMETERS*);
static PresentFn g_present = nullptr;
static Present1Fn g_present1 = nullptr;
static thread_local bool t_in = false;
static LARGE_INTEGER g_qf, g_last;
static int g_frames = 0;

static void OnPresent(IDXGISwapChain* sc) {
    if (t_in) return;
    t_in = true;
    __try {
        LARGE_INTEGER now;
        QueryPerformanceCounter(&now);
        g_frames++;
        if (now.QuadPart - g_last.QuadPart >= g_qf.QuadPart / 2) {
            g_fps = (int)(g_frames * (double)g_qf.QuadPart / (double)(now.QuadPart - g_last.QuadPart) + 0.5);
            g_frames = 0; g_last = now;
            g_dirty = true;
        }
        ULONGLONG t = GetTickCount64();
        if (t - g_cfgCheck > 1000) { g_cfgCheck = t; LoadCfg(); }
        ID3D11Device* dev = nullptr;
        if (SUCCEEDED(sc->GetDevice(__uuidof(ID3D11Device), (void**)&dev)) && dev) {
            Draw(sc, dev);
            dev->Release();
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        R.bad = true;  // bei jedem Fehler das Zeichnen abschalten, das Spiel laeuft ungestoert weiter
    }
    t_in = false;
}
static HRESULT STDMETHODCALLTYPE HkPresent(IDXGISwapChain* sc, UINT a, UINT b) { OnPresent(sc); return g_present(sc, a, b); }
static HRESULT STDMETHODCALLTYPE HkPresent1(IDXGISwapChain1* sc, UINT a, UINT b, const DXGI_PRESENT_PARAMETERS* p) { OnPresent(sc); return g_present1(sc, a, b, p); }

static void Patch(void** slot, void* fn) {
    DWORD old;
    if (VirtualProtect(slot, sizeof(void*), PAGE_EXECUTE_READWRITE, &old)) {
        *slot = fn;
        VirtualProtect(slot, sizeof(void*), old, &old);
    }
}
static DWORD WINAPI InitThread(LPVOID) {
    Sleep(3000);
    typedef HRESULT(WINAPI * PCreate)(IDXGIAdapter*, D3D_DRIVER_TYPE, HMODULE, UINT, const D3D_FEATURE_LEVEL*, UINT, UINT, const DXGI_SWAP_CHAIN_DESC*, IDXGISwapChain**, ID3D11Device**, D3D_FEATURE_LEVEL*, ID3D11DeviceContext**);
    HMODULE d3d = LoadLibraryW(L"d3d11.dll");
    PCreate create = d3d ? (PCreate)GetProcAddress(d3d, "D3D11CreateDeviceAndSwapChain") : nullptr;
    if (!create) return 0;
    WNDCLASSW wc = {};
    wc.lpfnWndProc = DefWindowProcW; wc.hInstance = GetModuleHandleW(nullptr); wc.lpszClassName = L"DHubDummy";
    RegisterClassW(&wc);
    HWND hw = CreateWindowExW(0, L"DHubDummy", L"", WS_OVERLAPPED, 0, 0, 100, 100, nullptr, nullptr, wc.hInstance, nullptr);
    DXGI_SWAP_CHAIN_DESC sd = {};
    sd.BufferCount = 1; sd.BufferDesc.Width = 100; sd.BufferDesc.Height = 100; sd.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT; sd.OutputWindow = hw; sd.SampleDesc.Count = 1; sd.Windowed = TRUE; sd.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;
    IDXGISwapChain* sc = nullptr; ID3D11Device* dev = nullptr; ID3D11DeviceContext* ctx = nullptr;
    D3D_FEATURE_LEVEL fl;
    if (SUCCEEDED(create(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0, D3D11_SDK_VERSION, &sd, &sc, &dev, &fl, &ctx)) && sc) {
        void** vt = *(void***)sc;
        g_present = (PresentFn)vt[8];
        IDXGISwapChain1* s1 = nullptr;
        if (SUCCEEDED(sc->QueryInterface(__uuidof(IDXGISwapChain1), (void**)&s1)) && s1) {
            g_present1 = (Present1Fn)vt[22];
            s1->Release();
        }
        Patch(&vt[8], (void*)HkPresent);
        if (g_present1) Patch(&vt[22], (void*)HkPresent1);
        sc->Release(); ctx->Release(); dev->Release();
    }
    DestroyWindow(hw);
    return 0;
}
BOOL APIENTRY DllMain(HMODULE h, DWORD why, LPVOID) {
    if (why == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(h);
        InitializeCriticalSection(&g_cs);
        wchar_t p[MAX_PATH];
        GetModuleFileNameW(h, p, MAX_PATH);
        g_dir = p;
        g_dir = g_dir.substr(0, g_dir.find_last_of(L"\\/") + 1);
        QueryPerformanceFrequency(&g_qf);
        QueryPerformanceCounter(&g_last);
        CloseHandle(CreateThread(nullptr, 0, InitThread, nullptr, 0, nullptr));
    }
    return TRUE;
}
