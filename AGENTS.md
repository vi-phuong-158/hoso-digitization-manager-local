# AGENTS.md — Số hóa hồ sơ Đảng viên

## 0. Vai trò của file này

Đây là hợp đồng vận hành của toàn bộ repo. Mọi Agent/model làm việc trong repo phải đọc file này trước.

Mục tiêu sau khi hoàn thiện:
- Opus/Agent mạnh dùng một lần để xây, kiểm thử và khóa logic.
- Gemini/Agent chạy thường xuyên chỉ thực thi pipeline đã khóa.
- Model runtime không được tự ý thay taxonomy, quy tắc đặt tên hoặc logic chia tài liệu.

Ưu tiên: **đúng > truy vết được > tự động hóa cao**.

---

## 1. Bài toán thực tế

Mỗi thư mục đầu vào thuộc **một người**.

Trong thư mục có nhiều PDF scan. Thứ tự file có thể lộn xộn.

**Một PDF KHÔNG đồng nghĩa với một tài liệu.**
Một PDF có thể chứa:
- một tài liệu một trang;
- một tài liệu nhiều trang;
- nhiều tài liệu độc lập;
- trang nội dung + bìa;
- mặt trước + mặt sau;
- nhiều văn bản cùng một loại nhưng khác ngày.

Pipeline bắt buộc phải xác định **ranh giới tài liệu ở cấp trang** trước khi đặt tên.

Ví dụ thực tế trong `test_cases/HAI_GOLDEN.json` là bộ acceptance test bắt buộc.

---

## 2. Nguồn chân lý

### 2.1 Taxonomy
`document_types.json` là nguồn chân lý duy nhất cho 104 loại tài liệu.

Classifier chỉ được trả:
- một `type_id` có trong catalog; hoặc
- `UNKNOWN`.

Không được tự tạo loại 105, không đổi mã, không đổi tên danh mục khi chưa có yêu cầu rõ ràng của người vận hành.

### 2.2 Golden test
`test_cases/HAI_GOLDEN.json` là bộ nhãn chuẩn ban đầu dùng để:
- kiểm thử tách tài liệu;
- kiểm thử phân loại;
- kiểm thử ghép bìa/mặt sau;
- kiểm thử đặt tên;
- phát hiện regression.

Không sửa golden label chỉ để làm test xanh. Nếu cho rằng golden label sai, phải báo riêng và chờ người vận hành duyệt.

---

## 3. Nguyên tắc bảo toàn hồ sơ

### MUST
- Giữ nguyên byte của PDF nguồn trong `input/`.
- Không rename/move/delete file nguồn.
- Khi tách PDF, lấy trực tiếp page objects từ PDF nguồn; không rasterize/recompress nếu không bắt buộc.
- Có SHA-256 cho file nguồn và file kết quả.
- Có manifest truy ngược được: source file + source pages -> logical document -> type -> output filename.
- Mọi trang nguồn phải thuộc đúng một logical document hoặc được đưa REVIEW.
- Không được có trang bị mất.
- Không được có trang bị dùng lặp ở hai logical document, trừ khi có quyết định REVIEW thủ công rõ ràng.

### MUST NOT
- Không sửa nội dung tài liệu.
- Không tự suy đoán lại chủ hồ sơ từ nội dung để chuyển sang thư mục khác.
- Không log toàn văn hồ sơ hoặc dữ liệu cá nhân không cần thiết.
- Không upload tài liệu ra dịch vụ khác ngoài model/provider đã được người vận hành cho phép.

---

## 4. Pipeline bắt buộc

### Phase A — Inventory
Với mỗi thư mục người:
1. Liệt kê toàn bộ PDF.
2. Đếm số trang.
3. Tính SHA-256.
4. Ghi inventory.
5. Không thay đổi file nguồn.

### Phase B — Page understanding
Đọc từng trang bằng PDF/Vision.

Với mỗi trang, tạo tín hiệu tối thiểu:
- số trang nguồn;
- tiêu đề/loại văn bản dự đoán;
- ngày văn bản nếu có;
- có phải bìa hay không;
- có phải mặt sau/trang tiếp tục hay không;
- ứng viên `type_id`;
- confidence;
- dấu hiệu bắt đầu tài liệu mới;
- dấu hiệu tiếp tục tài liệu trước/sau.

Không cần OCR toàn văn nếu Vision đủ dùng.

### Phase C — Document segmentation
Tạo các `logical_document` từ các trang.

Một logical document có thể là:
- `[1]`
- `[1,2]`
- `[3,4]`
- `[6,7]`

Các trang không nhất thiết có chữ:
- Bìa của bằng/chứng chỉ phải ghép với trang nội dung tương ứng.
- Mặt sau có dấu/xác nhận phải ghép với mặt trước.
- Không tách bìa thành tài liệu UNKNOWN nếu có bằng chứng mạnh nó thuộc tài liệu liền kề.

Nếu không chắc bìa thuộc trước hay sau -> REVIEW.

### Phase D — Document classification
Sau khi segmentation, classifier đọc **toàn bộ logical document**, không chỉ trang đầu.

Output schema bắt buộc:

```json
{
  "source_file": "example.pdf",
  "source_pages": [1, 2],
  "type_id": "86",
  "confidence": 0.97,
  "document_date": "2023-05-15",
  "date_confidence": 0.98,
  "title_short": "Bằng cử nhân Điều tra hình sự",
  "needs_review": false,
  "review_reason": null
}
```

Không chép toàn văn tài liệu vào JSON.

### Phase E — Naming
AI **không được tự nghĩ filename**.

Naming engine:
`type_id -> document_types.json -> filename_base -> sequence -> .pdf`

Nếu 1 tài liệu của loại:
`[filename_base].pdf`

Nếu nhiều tài liệu cùng loại:
`[filename_base].1.pdf`
`[filename_base].2.pdf`
...

Số thứ tự phải từ **cũ -> mới** dựa trên ngày văn bản đáng tin cậy.

Nếu không xác định được thứ tự thời gian đủ chắc:
- vẫn giữ segmentation/classification;
- đưa nhóm đó REVIEW;
- không tự đánh `.1/.2` theo thứ tự scan.

### Phase F — Write
Mặc định `dry-run`.

`dry-run`:
- tạo manifest dự kiến;
- không tạo output cuối.

`apply`:
- chỉ chạy khi manifest hợp lệ;
- tách/copy page objects sang `output/`;
- ca mơ hồ sang `review/`;
- không sửa `input/`.

### Phase G — Integrity/QC
Bắt buộc kiểm:
- page coverage = 100%;
- không overlap trang;
- số logical document hợp lý;
- output mở được;
- không collision filename;
- hash source giữ nguyên;
- naming chỉ lấy từ catalog;
- AUTO + REVIEW bao phủ toàn bộ logical documents.

---

## 5. Quy tắc confidence

Không dùng confidence tự khai của model như chân lý tuyệt đối; nó chỉ là một tín hiệu.

Mặc định:
- >= 0.95 và không có cờ rủi ro: AUTO.
- 0.80–0.949: second-pass review bằng model.
- < 0.80: HUMAN REVIEW.
- `UNKNOWN`: HUMAN REVIEW.
- segmentation ambiguity: HUMAN REVIEW.
- duplicate-type ordering ambiguity: HUMAN REVIEW.

Second pass phải nhận:
- ảnh/PDF logical document;
- top candidates;
- các mô tả taxonomy liên quan;
không nhận kết luận vòng 1 như một sự thật bắt buộc.

---

## 6. Những cặp/nhóm dễ nhầm

Phải kiểm tra kỹ:
- 01 vs 02
- 03 vs 85
- 05 vs 06
- 07 vs 09 vs 10
- 19 vs 72 vs 73
- 22 vs 36 vs 67
- 37 vs 39
- 43 vs 45
- 47 vs 61 vs 62 vs 63 vs 65
- 50 vs 52
- 54 vs 55 vs 56 vs 59
- 57 vs 58
- 70 vs 86
- 92 vs 93 vs 94 vs 95
- 98 vs 99
- 100 vs 60
- 103 vs 104

### 70 vs 86
- 70: bằng/chứng chỉ **lý luận chính trị**.
- 86: văn bằng/chứng chỉ chuyên môn, nghiệp vụ, ngoại ngữ, tin học, bồi dưỡng... và các văn bằng phù hợp nhóm này.
- **Bằng tốt nghiệp THPT**: Người vận hành đã chốt xếp vào **Mã 86**.

### Bộ hồ sơ Kết nạp Đảng & Tài liệu Đoàn (Chính sách đã chốt)
- **Bằng tốt nghiệp THPT** -> xếp vào **Mã 86** (Văn bằng, chứng chỉ).
- **Các tài liệu Đoàn giới thiệu vào Đảng** (Bản kiểm điểm đoàn viên, Biên bản kiểm phiếu tín nhiệm, Biên bản hội nghị chi đoàn, Báo cáo của chi đoàn đề nghị giới thiệu...) -> xếp vào **Mã 40** (`40.Nghi_quyet_gioi_thieu_doan_vien_uu_tu_vao_Dang`).
- **Bản kiểm điểm người xin vào Đảng (Mẫu 2B-KNĐ)** -> xếp vào **Mã 37** (`37.Don_xin_vao_Dang`).
- **Báo cáo kết nạp đảng viên sau khi tổ chức lễ kết nạp** -> xếp vào **Mã 05** (`05.Quyet_dinh_ket_nap_dang_vien`).
- Không để các tài liệu trên ở dạng `SUPPORTING_DOCUMENT` khi xử lý các hồ sơ tương tự.

---

## 7. Quy tắc với bìa và mặt sau

Bìa/mặt sau không phải tài liệu độc lập nếu:
- tiêu đề bìa khớp nội dung liền kề;
- màu/mẫu/chủ đề khớp;
- trình tự scan cho thấy cùng một văn bằng/chứng chỉ;
- không có nội dung độc lập khác.

Ví dụ trong golden test:
- trang bằng/chứng chỉ + trang bìa ngay sau có thể là một logical document;
- giấy khai sinh mặt trước + mặt sau có dấu xác nhận là một logical document.

Không được đổi thứ tự trang khi xuất.

---

## 8. Manifest chuẩn

Mỗi thư mục người tạo một manifest:

```json
{
  "person_folder": "NGUYEN_HUU_HAI",
  "sources": [
    {
      "file": "sample.pdf",
      "sha256": "...",
      "pages": 8
    }
  ],
  "documents": [
    {
      "source_file": "sample.pdf",
      "source_pages": [4],
      "type_id": "05",
      "confidence": 0.99,
      "document_date": "2018-08-19",
      "target_file": "05.Quyet_dinh_ket_nap_dang_vien.pdf",
      "status": "AUTO"
    }
  ],
  "qc": {
    "all_pages_accounted_for": true,
    "no_page_overlap": true,
    "filename_collision": false
  }
}
```

---

## 9. Runtime mode sau khi hệ thống đã khóa

Khi người vận hành yêu cầu **RUN/PROCESS/THỰC THI**:

Gemini/runtime Agent:
1. Không sửa source code.
2. Không sửa `AGENTS.md`.
3. Không sửa `document_types.json`.
4. Không sửa golden tests.
5. Chạy pipeline có sẵn ở `dry-run`.
6. Trình bày summary:
   - source files;
   - source pages;
   - logical documents;
   - AUTO;
   - REVIEW;
   - lỗi.
7. Chỉ `apply` nếu người vận hành yêu cầu rõ.
8. Nếu test/invariant fail: dừng, không tự vá code.

Muốn thay logic phải chuyển sang **DEV mode** bằng model/Agent được giao nhiệm vụ phát triển riêng.

---

## 10. DEV mode

Chỉ dùng khi người vận hành yêu cầu xây/sửa hệ thống.

Agent DEV:
- đọc `AGENTS.md`;
- đọc catalog;
- đọc golden tests;
- tạo plan ngắn;
- viết code tối giản;
- chạy unit tests;
- chạy golden acceptance;
- báo kết quả thật, không che regression.

Không thêm MarkItDown, RAG, Pinecone, vector DB, database server hay agent framework nếu chưa chứng minh cần thiết.

---

## 11. Acceptance gate

Không được tuyên bố pipeline "ổn" nếu chưa đạt:
- Golden segmentation: 100% page grouping đúng trên các case đã duyệt.
- Golden classification: 100% type đúng trên các case đã duyệt hoặc đúng REVIEW đối với case được đánh dấu mơ hồ.
- Page coverage: 100%.
- Page overlap: 0.
- Source mutation: 0.
- Filename từ catalog: 100%.
- Dry-run idempotent.
- Apply idempotent hoặc fail-safe, không tạo bản trùng âm thầm.

---

## 12. Nguyên tắc cuối

Nếu không chắc:
**REVIEW — KHÔNG ĐOÁN.**

Nếu test đỏ:
**DỪNG — KHÔNG TỰ HẠ TIÊU CHUẨN.**

Nếu runtime gặp ca mới:
**KHÔNG TỰ SỬA LUẬT; ghi nhận ca mới để DEV mode xử lý.**
