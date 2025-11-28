from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
import fitz  # PyMuPDF
import io

APP_TITLE = "PDF Header Remover API"
PT_PER_INCH = 72.0
MM_PER_INCH = 25.4

app = FastAPI(title=APP_TITLE)
origins = [
    "*",  
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def mm_to_pt(mm: float) -> float:
    return mm * PT_PER_INCH / MM_PER_INCH


def union_rects(rects):
    merged = []
    for r in rects:
        added = False
        for i, mr in enumerate(merged):
            if (r.x1 >= mr.x0 and r.x0 <= mr.x1 and r.y1 >= mr.y0 and r.y0 <= mr.y1):
                merged[i] = fitz.Rect(
                    min(mr.x0, r.x0),
                    min(mr.y0, r.y0),
                    max(mr.x1, r.x1),
                    max(mr.y1, r.y1),
                )
                added = True
                break
        if not added:
            merged.append(fitz.Rect(r))  # copy
    return merged


def process_pdf_bytes(
    pdf_bytes: bytes,
    terms_text: str,
    band_mm: float,
    margin_mm: float,
    ignore_case: bool,
) -> bytes:
    # terms_text: çok satırlı string, her satır bir header
    terms = [ln.strip() for ln in terms_text.splitlines() if ln.strip()]
    if not terms:
        raise ValueError("En az bir header metni girilmelidir.")

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise ValueError(f"PDF açılamadı: {e}")

    try:
        total = len(doc)
        band_h_pt = mm_to_pt(float(band_mm))
        margin_pt = mm_to_pt(float(margin_mm))

        flags = 0
        if ignore_case:
            flags |= fitz.TEXT_IGNORECASE

        for i in range(total):
            page = doc[i]
            page_rect = page.rect

            # Üst bant alanı
            band = fitz.Rect(
                page_rect.x0 + margin_pt,
                page_rect.y0,
                page_rect.x1 - margin_pt,
                page_rect.y0 + band_h_pt,
            )

            all_hits = []
            for t in terms:
                if not t:
                    continue
                found = []
                try:
                    # Önce clip ile dene
                    found = page.search_for(t, quads=False, clip=band, flags=flags)
                except TypeError:
                    # Eski PyMuPDF versiyonu için fallback
                    found = page.search_for(t, quads=False, flags=flags)
                    found = [r for r in found if r.intersects(band)]
                except Exception:
                    try:
                        found = page.search_for(t, quads=False, flags=flags)
                        found = [r for r in found if r.intersects(band)]
                    except Exception:
                        found = []

                all_hits.extend(found)

            if all_hits:
                # Rectleri birleştir ve redaction uygula
                for r in union_rects(all_hits):
                    page.add_redact_annot(r, fill=(1, 1, 1))
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        out_buf = io.BytesIO()
        doc.save(out_buf, deflate=True, garbage=4)
        doc.close()
        out_buf.seek(0)
        return out_buf.read()
    except Exception as e:
        try:
            doc.close()
        except Exception:
            pass
        raise ValueError(f"PDF işleme hatası: {e}")


@app.post("/remove-headers")
async def remove_headers(
    file: UploadFile = File(..., description="Girdi PDF dosyası"),
    header_texts: str = Form(
        ...,
        description="Silinecek header metinleri (her satırda bir tane)",
    ),
    band_mm: float = Form(25.0, description="Üst bant yüksekliği (mm)"),
    margin_mm: float = Form(0.0, description="Sol/Sağ margin (mm)"),
    ignore_case: bool = Form(False, description="Büyük/küçük harfe duyarsız ara"),
):
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=400, detail="Lütfen PDF dosyası yükleyin.")

    try:
        pdf_bytes = await file.read()
        result_bytes = process_pdf_bytes(
            pdf_bytes=pdf_bytes,
            terms_text=header_texts,
            band_mm=band_mm,
            margin_mm=margin_mm,
            ignore_case=ignore_case,
        )
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Sunucu hatası: {e}"})

    # Çıktı dosya adını oluştur
    base_name = file.filename.rsplit(".", 1)[0] if file.filename else "output"
    out_name = f"{base_name}_noheaders.pdf"

    return StreamingResponse(
        io.BytesIO(result_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{out_name}"'
        },
    )


@app.get("/")
def root():
    return {
        "message": "PDF Header Remover API çalışıyor.",
        "endpoint": "/remove-headers",
        "method": "POST",
    }
