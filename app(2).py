import base64
import io
import zipfile
from pathlib import Path

import streamlit as st

from ard2dxf import ArdError, convert

ROOT = Path(__file__).parent

st.set_page_config(page_title="ARD → DXF", page_icon="✂️", layout="centered")


@st.cache_data
def hero_b64():
    return base64.b64encode((ROOT / "assets" / "hero.jpg").read_bytes()).decode()


st.markdown(
    f"""
    <style>
      #MainMenu, footer, header {{visibility: hidden;}}
      .block-container {{padding-top: 2rem; max-width: 760px;}}
      .hero {{
        position: relative; border-radius: 14px; overflow: hidden; height: 260px;
        background: url("data:image/jpeg;base64,{hero_b64()}") center 60% / cover;
        margin-bottom: 2rem;
      }}
      .hero::after {{
        content: ""; position: absolute; inset: 0;
        background: linear-gradient(100deg, rgba(20,20,22,.82) 0%, rgba(20,20,22,.35) 60%, rgba(20,20,22,0) 100%);
      }}
      .hero-text {{position: absolute; z-index: 1; left: 2rem; bottom: 1.8rem; color: #fff;}}
      .hero-text h1 {{margin: 0; padding: 0; font-size: 2.3rem; font-weight: 600; letter-spacing: -.02em; color: #fff;}}
      .hero-text p {{margin: .35rem 0 0; font-size: 1rem; opacity: .85;}}
      .muted {{color: #7a7a7a; font-size: .85rem;}}
    </style>
    <div class="hero">
      <div class="hero-text">
        <h1>ARD → DXF</h1>
        <p>Convertissez vos tracés ArtiosCAD pour Picador</p>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

files = st.file_uploader(
    "Déposez un ou plusieurs fichiers .ARD",
    type=["ard"],
    accept_multiple_files=True,
)

if files:
    results, errors = [], []
    for f in files:
        try:
            dxf, n = convert(f.getvalue())
            results.append((Path(f.name).stem + ".dxf", dxf, n))
        except ArdError as e:
            errors.append((f.name, str(e)))
        except Exception as e:  # format inattendu : on n'arrête pas le lot
            errors.append((f.name, f"Erreur de lecture ({e.__class__.__name__})"))

    for name, msg in errors:
        st.error(f"**{name}** — {msg}")

    if results:
        st.success(f"{len(results)} fichier(s) converti(s)")
        with st.expander("Détail"):
            for name, _, n in results:
                st.write(f"`{name}` — {n} éléments")

        if len(results) == 1:
            name, dxf, _ = results[0]
            st.download_button("Télécharger le DXF", dxf.encode("ascii"), file_name=name,
                               mime="application/dxf", type="primary", width="stretch")
        else:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for name, dxf, _ in results:
                    z.writestr(name, dxf)
            st.download_button("Télécharger le ZIP", buf.getvalue(), file_name="conversions_dxf.zip",
                               mime="application/zip", type="primary", width="stretch")

st.markdown(
    '<p class="muted">Calques générés : <b>Cut</b> (coupe) et <b>Crease</b> (rainage). '
    "Les textes, cotes et ponts ne sont pas encore convertis. Les fichiers ne sont pas conservés.</p>",
    unsafe_allow_html=True,
)
