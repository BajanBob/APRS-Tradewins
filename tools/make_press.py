# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Build the 3.23 press releases (Spanish Sh, Portuguese Pt) as styled HTML pages.

    python tools/make_press.py          writes docs/press/Tradewind-3.23-<ed>-comunicado.html
Render each to PDF with any Chromium browser (Print > Save as PDF, A4, background graphics on).
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CSS = """
@page{size:A4;margin:0}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{font-family:"Segoe UI","Helvetica Neue",Arial,sans-serif;color:#1d2433;font-size:9.8pt;line-height:1.45;-webkit-print-color-adjust:exact;print-color-adjust:exact;background:#8a93a3}
.page{width:210mm;height:297mm;position:relative;overflow:hidden;background:#fff;margin:0 auto 6mm;break-after:page}
.page:last-child{break-after:auto;margin-bottom:0}
@media print{body{background:#fff}.page{margin:0}}
.hero{background:linear-gradient(135deg,#0a3a52 0%,#0e7c86 55%,#14a3a8 100%);color:#fff;padding:13mm 15mm 11mm;position:relative;overflow:hidden}
.hero svg{position:absolute;right:-18mm;top:-14mm;opacity:.18}
.kick{font-size:7.6pt;font-weight:800;letter-spacing:2.6px;text-transform:uppercase;color:#bdf0ee}
.brand{display:flex;justify-content:space-between;align-items:center;margin-bottom:6mm}
.brand b{font-size:15pt;letter-spacing:4px;font-weight:800}
.badge{background:#c9a227;color:#0d1b33;font-weight:800;font-size:7.8pt;letter-spacing:1px;padding:1.6mm 3.5mm;border-radius:20mm;text-transform:uppercase}
h1{font-size:23pt;line-height:1.1;margin:2mm 0 3mm;letter-spacing:-.5px;max-width:165mm}
.sub{font-size:11pt;color:#e3f7f6;max-width:160mm;margin:0}
.lead{padding:5mm 15mm 0;font-size:10.4pt}
.lead b{color:#0e7c86}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:3.5mm;padding:5mm 15mm 0}
.st{background:#e6f6f5;border-radius:3mm;padding:3mm 3.5mm;border-left:1.2mm solid #14a3a8}
.st b{display:block;font-size:15pt;color:#0e7c86;line-height:1.05}
.st span{font-size:7.8pt;color:#4b5565}
.cols{display:grid;grid-template-columns:1fr 58mm;gap:6mm;padding:4mm 15mm 0}
h2{font-size:12.5pt;color:#0d1b33;margin:0 0 2.5mm;display:flex;align-items:center;gap:2.5mm}
h2:before{content:"";width:1.4mm;height:5mm;background:#14a3a8;border-radius:1mm}
.feat{display:grid;grid-template-columns:1fr 1fr;gap:3mm}
.f{border:1px solid #d9dee7;border-radius:2.5mm;padding:2.8mm 3.2mm}
.f b{display:flex;gap:2mm;align-items:center;color:#0d1b33;font-size:9.4pt;margin-bottom:.8mm}
.f b i{font-style:normal;width:6.5mm;height:6.5mm;border-radius:1.8mm;background:#0e7c86;color:#fff;display:flex;align-items:center;justify-content:center;font-size:8.5pt}
.f span{font-size:8.5pt;color:#39424f}
.side{background:#0d1b33;color:#dfe5f1;border-radius:3mm;padding:4.5mm;font-size:8.6pt}
.side h3{color:#e6c35c;font-size:9pt;letter-spacing:1.6px;text-transform:uppercase;margin:0 0 3mm}
.side dl{margin:0}
.side dt{color:#8fe3df;font-size:7.4pt;letter-spacing:1px;text-transform:uppercase;margin-top:2.6mm}
.side dd{margin:.4mm 0 0;font-weight:600;color:#fff}
.foot{position:absolute;left:15mm;right:15mm;bottom:5mm;display:flex;justify-content:space-between;font-size:7.4pt;color:#7a8494;border-top:1px solid #d9dee7;padding-top:2.5mm}
.p2{padding:12mm 15mm 0}
.new{display:grid;grid-template-columns:1fr 1fr;gap:3.5mm;margin-bottom:5mm}
.n{background:#f4fbfb;border-radius:2.5mm;padding:3mm 3.5mm;border-top:1.1mm solid #14a3a8}
.n b{color:#0d1b33;display:block;margin-bottom:.8mm;font-size:9.4pt}
.n span{font-size:8.5pt;color:#39424f}
.tr{background:linear-gradient(135deg,#13213c,#1a2f59);border-radius:4mm;padding:5mm 6mm;color:#e9edf5;margin-bottom:5mm}
.tr h3{margin:0 0 1.5mm;color:#fff;font-size:12.5pt}
.tr p{margin:0 0 3mm;font-size:9pt;color:#c8d1e1}
.chat{display:grid;grid-template-columns:1fr 1fr;gap:5mm}
.bub{border-radius:3mm;padding:2.6mm 3.4mm;font-size:8.8pt;margin-bottom:2mm}
.bub small{display:block;font-size:7.2pt;opacity:.85;margin-top:1.4mm;border-top:1px dashed rgba(255,255,255,.3);padding-top:1mm}
.out{background:#0e7c86;color:#fff;border-radius:3mm 3mm .8mm 3mm}
.in{background:#2a3b5f;color:#fff;border-radius:3mm 3mm 3mm .8mm}
.who{font-size:7pt;letter-spacing:1px;font-weight:800;color:#8fe3df;margin-bottom:1mm}
.air{font-size:7.6pt;color:#e6c35c;font-weight:700;margin:1mm 0 0}
.eng{display:grid;grid-template-columns:repeat(4,1fr);gap:2.5mm;margin-top:3.5mm}
.e{background:rgba(255,255,255,.07);border-radius:2mm;padding:2.4mm 2.8mm}
.e b{display:block;color:#e6c35c;font-size:8.8pt}
.e span{font-size:7.6pt;color:#c8d1e1}
.two{display:grid;grid-template-columns:1fr 1fr;gap:5mm}
.box{border:1px solid #d9dee7;border-radius:3mm;padding:3.5mm 4mm;font-size:8.8pt}
.box.gold{border-color:#c9a227;background:#fbf8ee}
.dl{display:inline-block;background:#e6f6f5;color:#0e7c86;font-weight:800;padding:1mm 2.5mm;border-radius:1.5mm;font-size:8.6pt;margin-top:1mm}
.contact{position:absolute;left:0;right:0;bottom:0;background:#0d1b33;color:#fff;padding:6mm 15mm 7mm;display:flex;justify-content:space-between;align-items:center}
.contact b{font-size:11pt}
.contact .mail{background:#c9a227;color:#0d1b33;font-weight:800;padding:2mm 4mm;border-radius:2mm;font-size:10.5pt}
.contact small{display:block;color:#9fb0cc;font-size:8pt;margin-top:1mm}
.end{color:#0e7c86;font-weight:700;margin-top:4mm}
.band{margin:4mm 15mm 0;background:linear-gradient(135deg,#0d1b33,#162a4d);border-radius:4mm;padding:5mm 6mm;color:#fff;display:flex;gap:5mm;align-items:center}
.band .big{font-size:26pt;line-height:1}
.band p{margin:0;font-size:12pt;font-weight:600;line-height:1.3}
.band p small{display:block;font-size:8.6pt;font-weight:400;color:#b9c4d8;margin-top:1.2mm}
.works{margin:4mm 15mm 0}
.works .lab{font-size:7.4pt;letter-spacing:1.8px;text-transform:uppercase;color:#0e7c86;font-weight:800;margin-bottom:2mm}
.chips{display:flex;flex-wrap:wrap;gap:2mm}
.chips span{background:#f1f4f8;border:1px solid #d9dee7;border-radius:10mm;padding:.8mm 2.6mm;font-size:7.8pt;color:#1d2433}
.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:3.5mm;margin-top:5mm}
.sp{border-radius:3mm;padding:3.2mm 3.6mm;background:#e6f6f5}
.sp b{display:block;font-size:17pt;color:#0e7c86;line-height:1}
.sp span{font-size:8.5pt;color:#1d2433}
"""

WAVES = """<svg width="110mm" height="110mm" viewBox="0 0 200 200" aria-hidden="true"><g fill="none" stroke="#fff" stroke-width="1.3">
<circle cx="100" cy="100" r="95"/><ellipse cx="100" cy="100" rx="42" ry="95"/><ellipse cx="100" cy="100" rx="76" ry="95"/>
<path d="M5 100h190M15 55h170M15 145h170"/><path d="M35 125 Q80 40 160 75" stroke="#e6c35c" stroke-width="2.4" stroke-dasharray="4 4"/>
<circle cx="35" cy="125" r="4" fill="#e6c35c" stroke="none"/><circle cx="160" cy="75" r="4" fill="#e6c35c" stroke="none"/></g></svg>"""

T = {
 "Sh": dict(lang="es", kicker="Comunicado de prensa", badge="Edición en Español · Sh",
  title="Tradewind 3.23 llega en español", sub="La estación APRS gratuita del Caribe, ahora para Argentina y toda Hispanoamérica, con traducción automática del chat",
  lead="<b>Bridgetown, Barbados · noviembre de 2026.</b> Los radioaficionados <b>Brandon St Rose (8P5DG)</b> y <b>Kammie Holder (8P5AB)</b> presentan <b>Tradewind 3.23 Edición en Español</b>: su aplicación libre y gratuita para radioaficionados, ahora con todo el panel en español y configurada para Argentina desde el primer arranque.",
  stats=[("1.100+","textos del panel en español"),("144.930","MHz · APRS en Argentina"),("4","plataformas: Windows, Linux, Pi, Android"),("Gratis","software libre, Licencia MIT")],
  h_what="Qué es Tradewind",
  feats=[("📡","Propagación en vivo","Vigila 2 m y 70 cm (TEP, conducto, esporádica E) y avisa cuando se abre una banda."),
         ("🎛","TNC, iGate y digipeater","La tarjeta de sonido hace de módem: no necesita Direwolf."),
         ("💬","Chat por RF","Chat bidireccional con acuse de recibo, relé y almacenamiento y reenvío."),
         ("📋","Emergencias","SITREP, ICS-213, refugios, daños, correo por radio y registro ICS-309."),
         ("🐺","Modo Lobo","Caza DX en APRS cuando PSKReporter muestra el trayecto abierto."),
         ("🛰","Satélites","Pases de la ISS, imágenes SSTV, telemetría y llamadas HF selectivas.")],
  side_t="Datos clave", side=[("Versión","Tradewind 3.23 · Sh"),("Idioma","Español (también portugués e inglés)"),("Zona inicial","Argentina / Uruguay"),("APRS","144.930 MHz"),("Plataformas","Windows · Linux · Raspberry Pi · Android"),("Licencia","MIT · gratuito"),("Sin internet","Sí: mapas guardados, RF pura")],
  h_new="Novedades de la versión 3.23",
  news=[("Todo el panel en español","Más de 1.100 textos, incluida la ayuda de cada ajuste al pasar el cursor."),
        ("Lista para Argentina","Arranca en la zona Argentina / Uruguay con APRS en 144.930 MHz; se cambia en el menú."),
        ("Selector de idioma","MENÚ › ESTACIÓN: español, portugués de Brasil o inglés; o automático según el teléfono."),
        ("Lo que sale al aire, intacto","Indicativos, códigos Q, cuadrículas y frecuencias nunca se traducen; balizas y boletines siguen en inglés.")],
  tr_t="🌐 Motor de traducción del chat",
  tr_p="El inglés sigue siendo el idioma en el aire para la comunicación mundial. Tradewind traduce por usted y siempre muestra el original.",
  me="LU1XX · USTED", them="PY2XX · BRASIL",
  out_txt="We need water at the Rosario shelter", out_note="Enviado en inglés · usted escribió: <i>Necesitamos agua en el refugio de Rosario</i>",
  out_air="Al aire: en inglés",
  in_txt="El camino al refugio está bloqueado, use la ruta norte", in_note="Traducido del inglés · original: <i>Road to the shelter is blocked, use the north road</i>",
  in_air="En pantalla: en español",
  engines=[("MyMemory","internet, sin cuenta"),("LibreTranslate","servidor propio: funciona sin internet en la red local"),("DeepL","la mejor calidad, con clave API"),("Argos","totalmente sin conexión en la PC")],
  h_av="Disponibilidad", av="Tradewind es <b>software libre y gratuito</b> para Windows, Linux, Raspberry Pi y Android.",
  dl_l="Descarga", dl="github.com/BajanBob/APRS-Tradewins/releases", files="Archivos <b>Tradewind-3.23-…-Sh.zip</b>",
  h_cx="Para organismos de emergencia", cx="<b>Tradewind Commander</b> es la edición comercial para organismos que operan en canales comerciales con licencia (148–174 MHz) y necesitan chat cifrado AES-256.",
  note="Use Tradewind solo en las frecuencias y con la potencia que permite su licencia, y respete el plan de banda de su sociedad nacional. La traducción automática puede equivocarse: en una emergencia, confirme todo lo importante.",
  c_t="Contacto de prensa", c_who="Kammie Holder (8P5AB) y Brandon St Rose (8P5DG) · Barbados", end="73 y buenas aperturas.",
  foot_l="Tradewind 3.23 · Edición en Español", foot_r="Página",
  band="Cuando fallan la red eléctrica e internet, Tradewind sigue comunicando por radio.", band_s="Chat, formularios de emergencia y mapas guardados funcionan solo con RF, en una notebook o una Raspberry Pi.",
  works="Funciona con lo que ya tiene",
  h_start="Empezar en 3 pasos", steps=["Descargue el zip <strong>-Sh</strong> de su plataforma y descomprímalo.", "Ábralo: el asistente pide su indicativo y su cuadrícula, y elige 144.930 MHz.", "Pulse el botón de encendido y llame a un amigo desde el chat."]),
 "Pt": dict(lang="pt", kicker="Comunicado à imprensa", badge="Edição em Português · Pt",
  title="Tradewind 3.23 chega em português", sub="A estação APRS gratuita do Caribe, agora para o Brasil, com tradução automática do chat",
  lead="<b>Bridgetown, Barbados · novembro de 2026.</b> Os radioamadores <b>Brandon St Rose (8P5DG)</b> e <b>Kammie Holder (8P5AB)</b> apresentam o <b>Tradewind 3.23 Edição em Português</b>: o aplicativo livre e gratuito deles para radioamadores, agora com todo o painel em português do Brasil e configurado para o Brasil desde a primeira vez que é aberto.",
  stats=[("1.100+","textos do painel em português"),("145.570","MHz · APRS no Brasil"),("4","plataformas: Windows, Linux, Pi, Android"),("Grátis","software livre, Licença MIT")],
  h_what="O que é o Tradewind",
  feats=[("📡","Propagação ao vivo","Vigia 2 m e 70 cm (TEP, duto, esporádica E) e avisa quando uma banda abre."),
         ("🎛","TNC, iGate e digipeater","A placa de som faz o papel de modem: não precisa do Direwolf."),
         ("💬","Chat por RF","Chat bidirecional com confirmação de entrega, relé e armazenamento e reenvio."),
         ("📋","Emergências","SITREP, ICS-213, abrigos, danos, e-mail por rádio e registro ICS-309."),
         ("🐺","Modo Lobo","Caça DX no APRS quando o PSKReporter mostra o caminho aberto."),
         ("🛰","Satélites","Passagens da ISS, imagens SSTV, telemetria e chamadas HF seletivas.")],
  side_t="Dados principais", side=[("Versão","Tradewind 3.23 · Pt"),("Idioma","Português do Brasil (também espanhol e inglês)"),("Zona inicial","Brasil"),("APRS","145.570 MHz"),("Plataformas","Windows · Linux · Raspberry Pi · Android"),("Licença","MIT · gratuito"),("Sem internet","Sim: mapas salvos, RF pura")],
  h_new="Novidades da versão 3.23",
  news=[("Todo o painel em português","Mais de 1.100 textos, incluindo a ajuda de cada ajuste ao passar o cursor."),
        ("Pronto para o Brasil","Começa na zona do Brasil com APRS em 145.570 MHz; muda-se no menu."),
        ("Seletor de idioma","MENU › ESTAÇÃO: português do Brasil, espanhol ou inglês; ou automático pelo celular."),
        ("O que vai ao ar, intacto","Indicativos, códigos Q, quadrículas e frequências nunca são traduzidos; beacons e boletins continuam em inglês.")],
  tr_t="🌐 Motor de tradução do chat",
  tr_p="O inglês continua sendo o idioma no ar para a comunicação mundial. O Tradewind traduz por você e sempre mostra o original.",
  me="PY2XX · VOCÊ", them="LU1XX · ARGENTINA",
  out_txt="Road to the shelter is blocked, use the north road", out_note="Enviado em inglês · você escreveu: <i>A estrada para o abrigo está bloqueada, use a rota norte</i>",
  out_air="No ar: em inglês",
  in_txt="Precisamos de água no abrigo de Rosario", in_note="Traduzido do inglês · original: <i>We need water at the Rosario shelter</i>",
  in_air="Na tela: em português",
  engines=[("MyMemory","internet, sem conta"),("LibreTranslate","servidor próprio: funciona sem internet na rede local"),("DeepL","a melhor qualidade, com chave de API"),("Argos","totalmente offline no PC")],
  h_av="Disponibilidade", av="O Tradewind é <b>software livre e gratuito</b> para Windows, Linux, Raspberry Pi e Android.",
  dl_l="Download", dl="github.com/BajanBob/APRS-Tradewins/releases", files="Arquivos <b>Tradewind-3.23-…-Pt.zip</b>",
  h_cx="Para órgãos de emergência", cx="O <b>Tradewind Commander</b> é a edição comercial para órgãos que operam em canais comerciais licenciados (148–174 MHz) e precisam de chat criptografado AES-256.",
  note="Use o Tradewind só nas frequências e com a potência que a sua licença permite, e respeite o plano de banda da LABRE. A tradução automática pode errar: numa emergência, confirme tudo o que for importante.",
  c_t="Contato para a imprensa", c_who="Kammie Holder (8P5AB) e Brandon St Rose (8P5DG) · Barbados", end="73 e boas aberturas.",
  foot_l="Tradewind 3.23 · Edição em Português", foot_r="Página",
  band="Quando a energia elétrica e a internet caem, o Tradewind continua comunicando por rádio.", band_s="Chat, formulários de emergência e mapas salvos funcionam só com RF, num notebook ou num Raspberry Pi.",
  works="Funciona com o que você já tem",
  h_start="Comece em 3 passos", steps=["Baixe o zip <strong>-Pt</strong> da sua plataforma e descompacte.", "Abra: o assistente pede o seu indicativo e a sua quadrícula, e escolhe 145.570 MHz.", "Pressione o botão liga/desliga e chame um amigo pelo chat."]),
}


def page(d):
    stats = "".join(f'<div class="st"><b>{a}</b><span>{b}</span></div>' for a, b in d["stats"])
    feats = "".join(f'<div class="f"><b><i>{i}</i>{t}</b><span>{x}</span></div>' for i, t, x in d["feats"])
    side = "".join(f"<dt>{a}</dt><dd>{b}</dd>" for a, b in d["side"])
    news = "".join(f'<div class="n"><b>{a}</b><span>{b}</span></div>' for a, b in d["news"])
    chips = "".join(f"<span>{c}</span>" for c in ["Icom IC-9100 · IC-705 · IC-7300", "Yaesu FT-991A · FTM-400", "Kenwood TH-D74 / D75", "Baofeng · Quansheng · Radtel", "Digirig · SignaLink · AIOC", "Direwolf · UZ7HO · VARA FM · RTL-SDR"])
    steps = "".join(f'<div class="sp"><b>{i}</b><span>{t}</span></div>' for i, t in enumerate(d["steps"], 1))
    eng = "".join(f'<div class="e"><b>{a}</b><span>{b}</span></div>' for a, b in d["engines"])
    return f"""<!DOCTYPE html><html lang="{d['lang']}"><head><meta charset="utf-8"><title>{d['title']}</title><style>{CSS}</style></head><body>
<section class="page">
 <div class="hero">{WAVES}
  <div class="brand"><b>TRADEWIND</b><span class="badge">{d['badge']}</span></div>
  <div class="kick">{d['kicker']}</div>
  <h1>{d['title']}</h1><p class="sub">{d['sub']}</p>
 </div>
 <div class="lead">{d['lead']}</div>
 <div class="stats">{stats}</div>
 <div class="cols">
  <div><h2>{d['h_what']}</h2><div class="feat">{feats}</div></div>
  <aside class="side"><h3>{d['side_t']}</h3><dl>{side}</dl></aside>
 </div>
 <div class="band"><div class="big">⚡</div><p>{d['band']}<small>{d['band_s']}</small></p></div>
 <div class="works"><div class="lab">{d['works']}</div><div class="chips">{chips}</div></div>
 <div class="foot"><span>{d['foot_l']}</span><span>{d['foot_r']} 1 / 2</span></div>
</section>
<section class="page">
 <div class="p2">
  <h2>{d['h_new']}</h2>
  <div class="new">{news}</div>
  <div class="tr"><h3>{d['tr_t']}</h3><p>{d['tr_p']}</p>
   <div class="chat">
    <div><div class="who">{d['me']}</div><div class="bub out">{d['out_txt']}<small>{d['out_note']}</small></div><div class="air">▲ {d['out_air']}</div></div>
    <div><div class="who">{d['them']}</div><div class="bub in">{d['in_txt']}<small>{d['in_note']}</small></div><div class="air">▼ {d['in_air']}</div></div>
   </div>
   <div class="eng">{eng}</div>
  </div>
  <div class="two">
   <div class="box"><h2>{d['h_av']}</h2>{d['av']}<br><span class="dl">{d['dl_l']}: {d['dl']}</span><br><span style="font-size:8.2pt;color:#4b5565">{d['files']}</span></div>
   <div class="box gold"><h2>{d['h_cx']}</h2>{d['cx']}</div>
  </div>
  <h2 style="margin-top:5mm">{d['h_start']}</h2><div class="steps" style="margin-top:0">{steps}</div>
  <p style="font-size:8.2pt;color:#5b6577;margin-top:4mm">{d['note']}</p>
  <p class="end">{d['end']}</p>
 </div>
 <div class="contact"><div><b>{d['c_t']}</b><small>{d['c_who']}</small></div><div class="mail">dmr8p5ab@gmail.com</div></div>
</section>
</body></html>"""


def main():
    out = os.path.join(ROOT, "docs", "press")
    os.makedirs(out, exist_ok=True)
    for ed, d in T.items():
        p = os.path.join(out, f"Tradewind-3.23-{ed}-comunicado.html")
        with open(p, "w", encoding="utf-8") as f:
            f.write(page(d))
        print(p)


if __name__ == "__main__":
    main()
