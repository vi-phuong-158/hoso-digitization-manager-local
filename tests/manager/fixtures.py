from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter


def pdf(path: Path, pages: int = 1, valid: bool = True) -> None:
    if not valid:
        path.write_bytes(b"not a pdf")
        return
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    with path.open("wb") as handle:
        writer.write(handle)


def build_fixture_tree(root: Path) -> dict[str, Path]:
    """Build the 10 synthetic pilot cases required by the handoff."""
    root.mkdir(parents=True, exist_ok=True)
    standard = root / "25.000.036.001.015_012345678901_Nguyen_Van_A"; standard.mkdir()
    pdf(standard / "01.Ly_lich_nguoi_xin_vao_dang.pdf")
    malformed = root / "Folder sai"; malformed.mkdir(); pdf(malformed / "not-a-document.pdf")
    named = root / "25.000.036.001.016_012345678902_Le_Thi_B"; named.mkdir(); pdf(named / "04.Phieu_bo_sung_ho_so_dang_vien.pdf")
    multi = root / "25.000.036.001.017_012345678903_Tran_Van_C"; multi.mkdir(); pdf(multi / "55.Giay_gioi_thieu_sinh_hoat_dang_tam_thoi.1.pdf"); pdf(multi / "55.Giay_gioi_thieu_sinh_hoat_dang_tam_thoi.2.pdf")
    unknown = root / "25.000.036.001.018_012345678904_Pham_Van_D"; unknown.mkdir(); pdf(unknown / "999.tai_lieu_la.pdf")
    duplicate = root / "25.000.036.001.019_012345678905_Hoang_Van_E"; duplicate.mkdir(); pdf(duplicate / "01.Ly_lich_nguoi_xin_vao_dang.pdf"); (duplicate / "01.Ly_lich_nguoi_xin_vao_dang.1.pdf").write_bytes((duplicate / "01.Ly_lich_nguoi_xin_vao_dang.pdf").read_bytes())
    missing_p1 = root / "25.000.036.001.020_012345678906_Ngo_Van_F"; missing_p1.mkdir()
    no_file = root / "25.000.036.001.021_012345678907_Bui_Van_G"; no_file.mkdir()
    broken = root / "25.000.036.001.022_012345678908_Do_Van_H"; broken.mkdir(); pdf(broken / "01.Ly_lich_nguoi_xin_vao_dang.pdf", valid=False)
    completed = root / "25.000.036.001.023_012345678909_Vu_Van_I"; completed.mkdir(); pdf(completed / "01.Ly_lich_nguoi_xin_vao_dang.pdf")
    return {"standard": standard, "malformed": malformed, "multi": multi, "unknown": unknown, "duplicate": duplicate, "missing_p1": missing_p1, "no_file": no_file, "broken": broken, "completed": completed}
