"""
TTS AssetBundle Converter
-------------------------
Upload a Unity AssetBundle and extract its Meshes (.obj), Textures (.png)
and, optionally, Collider geometry / parameters.
"""

import io
import json
import zipfile
import traceback
from collections import Counter

import streamlit as st
import UnityPy

st.set_page_config(page_title="TTS AssetBundle Converter", page_icon="🧩", layout="wide")

# ---------------------------------------------------------------- helpers ---

def safe_name(raw, fallback):
    """Make a filesystem-safe, non-empty name."""
    name = (raw or "").strip() or fallback
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def uniquify(name, seen):
    """Avoid clobbering when two assets share a name."""
    base = name
    i = 1
    while name in seen:
        name = f"{base}_{i}"
        i += 1
    seen.add(name)
    return name


def extract(bundle_bytes, want_mesh, want_tex, want_collider, want_material):
    """Return (dict[path->bytes], summary_rows, previews[list[(name,png_bytes)]])."""
    env = UnityPy.load(bundle_bytes)

    files = {}          # path in zip -> bytes
    rows = []           # summary table
    previews = []       # (name, png bytes) for texture preview
    seen = set()
    type_counts = Counter()

    for obj in env.objects:
        t = obj.type.name
        type_counts[t] += 1
        try:
            data = obj.read()
            name = safe_name(getattr(data, "m_Name", ""), f"{t}_{obj.path_id}")

            # ---- textures -------------------------------------------------
            if want_tex and t in ("Texture2D", "Sprite"):
                img = data.image                      # PIL.Image
                if img is None:
                    rows.append((t, name, "skipped (no image data)"))
                    continue
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                fn = uniquify(f"textures/{name}.png", seen)
                files[fn] = buf.getvalue()
                rows.append((t, name, f"{img.width}x{img.height} PNG"))
                if len(previews) < 12:
                    previews.append((name, buf.getvalue()))

            # ---- meshes ---------------------------------------------------
            elif want_mesh and t == "Mesh":
                obj_text = data.export()              # OBJ string (default)
                fn = uniquify(f"meshes/{name}.obj", seen)
                files[fn] = obj_text.encode("utf-8")
                rows.append((t, name, f"{len(obj_text.splitlines())} obj lines"))

            # ---- colliders ------------------------------------------------
            elif want_collider and t == "MeshCollider":
                mesh_ref = getattr(data, "m_Mesh", None)
                note = f"convex={bool(getattr(data, 'm_Convex', 0))}"
                if mesh_ref is not None:
                    try:
                        cmesh = mesh_ref.read()       # dereference PPtr[Mesh]
                        ctext = cmesh.export()
                        cname = safe_name(getattr(cmesh, "m_Name", ""), f"collider_{obj.path_id}")
                        fn = uniquify(f"colliders/{cname}.obj", seen)
                        files[fn] = ctext.encode("utf-8")
                        note += " -> mesh exported"
                    except Exception as e:
                        note += f" (mesh deref failed: {e})"
                rows.append((t, name, note))

            elif want_collider and t in ("BoxCollider", "SphereCollider", "CapsuleCollider"):
                # Primitive colliders are just parameters -> dump as JSON.
                params = {
                    k: str(getattr(data, k))
                    for k in ("m_Center", "m_Size", "m_Radius", "m_Height", "m_IsTrigger")
                    if hasattr(data, k)
                }
                fn = uniquify(f"colliders/{t}_{name}.json", seen)
                files[fn] = json.dumps(params, indent=2, default=str).encode("utf-8")
                rows.append((t, name, "parameters -> json"))

            # ---- materials (best effort: which texture is _MainTex) -------
            elif want_material and t == "Material":
                info = {"name": name}
                try:
                    props = data.m_SavedProperties
                    tex_map = {}
                    for pair in getattr(props, "m_TexEnvs", []) or []:
                        # pair is usually (name, TexEnv{ m_Texture PPtr })
                        pname = pair[0] if isinstance(pair, (list, tuple)) else getattr(pair, "first", "")
                        tenv = pair[1] if isinstance(pair, (list, tuple)) else getattr(pair, "second", None)
                        try:
                            tex = tenv.m_Texture.read()
                            tex_map[str(pname)] = safe_name(getattr(tex, "m_Name", ""), "?")
                        except Exception:
                            tex_map[str(pname)] = None
                    info["textures"] = tex_map
                except Exception as e:
                    info["error"] = str(e)
                fn = uniquify(f"materials/{name}.json", seen)
                files[fn] = json.dumps(info, indent=2, default=str).encode("utf-8")
                rows.append((t, name, "material map -> json"))

        except Exception as e:
            rows.append((t, f"path_id={obj.path_id}", f"ERROR: {e}"))

    return files, rows, previews, dict(type_counts)


# ----------------------------------------------------------------- UI -------

st.title("🧩 TTS AssetBundle → Mesh / Texture Converter")
st.caption(
    "Extracts geometry and textures from Unity AssetBundles so you can rebuild "
    "them as normal TTS custom models. Nothing is uploaded anywhere — extraction "
    "runs in the app process."
)

with st.sidebar:
    st.header("What to extract")
    want_mesh = st.checkbox("Meshes (.obj)", value=True)
    want_tex = st.checkbox("Textures (.png)", value=True)
    want_collider = st.checkbox("Colliders (.obj / .json)", value=False)
    want_material = st.checkbox("Material → texture map (.json)", value=False)
    st.markdown("---")
    st.caption(f"UnityPy {UnityPy.__version__}")

uploaded = st.file_uploader(
    "Drop a Unity AssetBundle",
    type=None,  # TTS bundles often have no / arbitrary extension
    help="Usually .unity3d or extensionless. Anything Unity's bundle format is fine.",
)

if uploaded is not None:
    if not any([want_mesh, want_tex, want_collider, want_material]):
        st.warning("Pick at least one asset type in the sidebar.")
        st.stop()

    with st.spinner("Parsing bundle…"):
        try:
            files, rows, previews, counts = extract(
                uploaded.getvalue(), want_mesh, want_tex, want_collider, want_material
            )
        except Exception:
            st.error("Failed to parse this file as a Unity AssetBundle.")
            st.code(traceback.format_exc())
            st.stop()

    st.subheader("Contents found")
    st.write(counts)

    if not files:
        st.info("No exportable assets matched your selection.")
    else:
        # Bundle into a zip in memory
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, blob in files.items():
                zf.writestr(path, blob)

        st.download_button(
            "⬇️ Download extracted assets (.zip)",
            data=zip_buf.getvalue(),
            file_name=f"{uploaded.name}_extracted.zip",
            mime="application/zip",
            type="primary",
        )

        if previews:
            st.subheader("Texture preview")
            cols = st.columns(4)
            for i, (name, png) in enumerate(previews):
                with cols[i % 4]:
                    st.image(png, caption=name, use_container_width=True)

    with st.expander("Extraction log"):
        st.dataframe(
            {"Type": [r[0] for r in rows],
             "Name": [r[1] for r in rows],
             "Result": [r[2] for r in rows]},
            use_container_width=True,
        )
else:
    st.info("Upload a bundle to begin.")