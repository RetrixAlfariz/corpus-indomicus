# L1 structure-first: hasil pengujian pada corpus nyata

Tanggal: 17 September 2026. Prototipe: 0.2.0.
Status: **complete_requires_quality_review**, bukan konversi produksi.

## Kode, input dan reproduksi independen

Kode berada di `tools/l1/`, branch `research/l1-structure-first`, commit implementasi `be330cb78f86da0c9b0ef8a6af916a6d36151a57`. Branch berasal dari `v1` pada `2eb218b868fd0305bde11b5b7b253355534eb240`. Pipeline acquisition, `v1`, `main`, dan lock dependency aplikasi tidak diubah.

[Draft PR #1](https://github.com/RetrixAlfariz/corpus-indomicus/pull/1)

[GitHub Actions: pengujian lengkap berhasil](https://github.com/RetrixAlfariz/corpus-indomicus/actions/runs/35218274725)

Input: **10 PDF asli, 728 halaman, 46.684.304 byte**. Ini bukan data sintetis. Sumber dipulihkan dari artefak `.pdf.gz` yang sudah di-commit dalam `reports/storage-study-20260916/benchmark_artifacts`, lalu SHA-256 dan ukurannya dicocokkan dengan `input_manifest.json` studi tersebut. Tidak ada acquisition baru, OCR ulang, atau penghapusan sumber.

Run lokal dan GitHub Actions sama-sama selesai. **43 tes lulus** pada masing-masing lingkungan. Seluruh **60 package L1** yang dihasilkan di CI memiliki SHA-256 yang sama dengan 60 package hasil lokal. Seluruh sumber tetap identik setelah pengujian. Error conversion: **0**.

Artefak CI bernama `l1-structure-study` berisi kode persis yang dijalankan, laporan, CSV, manifest, provenance, package, preview, dan XML hasil tes. SHA-256 ZIP artefak: `8949668aa3c768175c7edf316761cc68e8c44a70dd04e092453c0784097ad3e3`. Retensi artefak CI tujuh hari; temuan ringkas ini tetap tersimpan di Git. Waktu run lokal sekitar 439,56 detik; CI sekitar 335,32 detik, masing-masing dua worker. Ini waktu pipeline satu pass, bukan microbenchmark codec.

## Hasil ukuran lengkap

Semua angka memakai MB desimal. Total package memasukkan metadata terkompresi, seluruh asset yang dipilih, index asset, manifest, serta header ZIP.

| Representation | Total byte | MB | Saving agregat |
|---|---:|---:|---:|
| PDF asli | 46.684.304 | 46,684 | 0% |
| Guarded / JSON entries | 19.040.870 | 19,041 | 59,214% |
| Guarded / channel pack | 17.144.893 | 17,145 | 63,275% |
| Guarded / flow pack | 16.863.350 | 16,863 | 63,878% |
| Vector candidate / JSON entries | 10.053.631 | 10,054 | 78,465% |
| Vector candidate / channel pack | 7.510.272 | 7,510 | 83,913% |
| Vector candidate / flow pack | **7.228.195** | **7,228** | **84,517%** |

Sebagai pembanding historis, studi sebelumnya pada input yang sama melaporkan 7z/LZMA2 sebesar **35.369.533 byte, saving 24,237%**. Ini bukan rerun codec dalam eksperimen L1. Fidelity juga berbeda: 7z mengembalikan PDF asli; L1 tidak.

`Guarded` mempertahankan envelope tabel yang terdeteksi sebagai crop raster. `Vector candidate` menggambar ulang garis panjang di dalam envelope grid tabel dan menempatkan teks hasil ekstraksi; kandidat ini lebih agresif dan belum mendapat persetujuan kualitas.

JSON entries menyimpan asset sebagai entry ZIP terpisah. Channel pack menggunakan channel metadata MessagePack+zstd serta satu `assets.bin` ber-index. Flow pack menghilangkan geometri baris dan menggabungkan teks pada halaman non-tabel; posisi halaman kompleks tetap dipertahankan. Semua mode menyimpan text pool mentah yang sama, bukan teks hasil koreksi.

## Distribusi, bukan hanya best case

| Statistik saving per dokumen | Guarded flow | Vector flow |
|---|---:|---:|
| Worst | 30,125% | 58,749% |
| Mean tak berbobot | 71,095% | 81,513% |
| P10 | 32,772% | 77,625% |
| Median/P50 | 81,477% | 83,191% |
| P90 | 87,404% | 87,403% |
| Best | 88,090% | 88,089% |
| Agregat berbobot byte | 63,878% | 84,517% |

Dua dokumen bertabel memberi sebagian besar perbedaan: `30093fb7` (260 halaman) berubah dari 10.465.351 byte guarded menjadi 2.634.757 byte vector, sedangkan `55110592` (58 halaman) berubah dari 2.447.731 menjadi 670.271 byte. Saving besar tersebut belum membuktikan tabel direkonstruksi dengan semantik sel yang benar.

## Komposisi hasil

| Komponen flow pack | Guarded byte | Vector candidate byte |
|---|---:|---:|
| Asset visual | 15.309.506 | 5.282.841 |
| Teks, posisi/struktur, provenance dan index terkompresi | 1.548.431 | 1.939.940 |
| Header/frame container | 5.413 | 5.414 |
| Total | 16.863.350 | 7.228.195 |

931.847 karakter hasil ekstraksi dikompresi menjadi **180.237 byte** secara per-dokumen, atau **164.134 byte** dalam satu frame gabungan. Ini hanya diagnosis text-only: belum termasuk asset, layout, provenance, directory atau search index. Angka tersebut tidak boleh dijual sebagai ukuran L1 lengkap.

Packing asset mengurangi overhead container kandidat vektor dari **2.581.874 menjadi 5.414 byte**. Jadi keuntungan terbesar channel-pack di implementasi ini datang dari packing, bukan bukti bahwa MessagePack selalu lebih kecil. Metadata MessagePack+zstd sendiri sedikit lebih besar daripada JSON+zstd pada dataset ini. Flow layout menghemat 282.077 byte tambahan dibanding channel-pack posisi tetap.

Asset kandidat agresif sekitar 13,58% dari byte image stream sumber pada laporan lama. Itu rasio byte, **bukan area dan bukan evidence recall**. Kedua policy menandai 239 halaman sebagai kandidat tabel, bukan tabel yang sudah diaudit. Guarded menghasilkan 8.495 region dan vector candidate 11.408 region. Full-page fallback tersedia, tetapi tidak terpakai pada run ini.

## Implementasi yang sudah ada

- Selectable text tanpa normalization atau koreksi OCR diam-diam, text offsets dan source-page IDs.
- Kandidat peran BAB/Pasal/ayat berdasarkan aturan sederhana, bukan legal AST terverifikasi.
- Deteksi region dari kotak OCR, connected components, morphology dan envelope grid tabel.
- Proteksi band penutup/tanda tangan kandidat; dukungan `--protect` untuk region manual.
- Crop memakai PNG atau TIFF Group 4 berdasarkan ukuran aktual; tidak ada JPEG untuk bitonal dan tidak ada threshold ulang crop pixels.
- Decoder package, verifikasi asset, export teks, viewer reflow dan review posisi secara offline.
- CLI terbatas 20 PDF/256 MB secara default, input kosong/partial dibedakan, folder output harus baru, tanpa source deletion.

Native raster digunakan jika kondisi halaman memungkinkan. Fallback crop dari render halaman maksimum 200 DPI dicatat dalam `asset_basis`; crop pixel-exact relatif terhadap basis tersebut, bukan seluruh PDF. Tidak ada canonicalization logo/stempel/tanda tangan.

## Audit kualitas dan batas interpretasi

**100% roundtrip teks tidak berarti OCR 100% benar.** Sampel halaman pertama `4f94003a` menunjukkan salah OCR yang sudah ada seperti “FRESIDEN” dan “REPUIUK”. Konverter mempertahankannya, bukan memperbaiki atau menebak isi hukum.

**Asset yang didekode identik tidak berarti semua asset penting ditemukan.** Deteksi downsample 128 DPI dan masking kotak OCR bisa melewatkan detail kecil, kontras rendah, atau visual yang tertutup penuh oleh box teks. Evidence recall belum diukur.

Inspeksi visual dilakukan pada `4f94003a` halaman 1 dan 7 serta `30093fb7` halaman 131. Pada sampel halaman penutup, cap/tanda tangan dipertahankan dalam crop. Pada tabel, grid tergambar kembali, tetapi salah OCR angka/huruf dan hubungan sel belum tervalidasi. Viewer fixed-layout masih dapat menampilkan overlap crop/teks dan ketidaksempurnaan font. Viewer reflow lebih mudah dibaca, tetapi urutan ekstraksi tidak dijamin sama dengan reading order yang benar.

Tes regresi menemukan bahwa sebagian kurva stempel dapat lolos deteksi garis panjang. Perbaikan membatasi vektorisasi ke envelope tabel; tes bentuk stempel yang memotong text box kini lulus. Ini memperbaiki kasus yang diuji, bukan membuktikan seluruh stempel aman.

Belum diimplementasikan: semantic table/cell/rowspan extraction, hierarki hukum terverifikasi, classifier signature/stamp terlatih, OCR ground truth, learned templates, rANS/Re-Pair, atau integrasi ingestion/eviction produksi.

Semua hasil tetap:

```json
{"status":"requires_review","ocr_accuracy":null,"evidence_recall":null,"approved_for_source_deletion":false}
```

Ini bukan kewajiban menyimpan source-exact selamanya. Ini pengaman eksperimen sampai fidelity L1 yang dipilih benar-benar dibuktikan.

## Reproduksi

```powershell
uv run --with-requirements tools/l1/requirements.txt pytest tools/l1/test_l1.py -q
uv run --with-requirements tools/l1/requirements.txt python tools/l1/study.py --input data --output reports/l1-study-new --workers 2
```

Gunakan direktori baru yang terpisah dari subtree input. Jika local `data` sudah lebih besar, pilih subset eksplisit atau sesuaikan budget. Workflow menyediakan restorasi sepuluh input dari artefak gzip yang sudah di-commit ketika local data tidak ada.

```powershell
uv run --with-requirements tools/l1/requirements.txt python tools/l1/open_l1.py path/to/flow_pack.ildr --html viewer.html --text extracted.txt
```

Ukuran workspace eksperimen lebih besar daripada ukuran canonical karena menyimpan semua alternatif, PDF sumber, preview dan log. Search index, backup dan cache juga belum termasuk angka canonical.

## Implikasi

L1 sudah menghasilkan penghematan besar pada corpus nyata ini, bukan hanya teori. Dengan rasio kandidat 84,517%, proyeksi aritmetika 2 TB menjadi sekitar **309,66 GB**, tetapi tidak boleh digeneralisasikan ke 2 TB dokumen lain tanpa sampling representatif. Kebijakan guarded pada rasio saat ini sekitar 722,44 GB.

Prioritas berikutnya adalah audit teks/region pada subset teranotasi, aturan tabel yang lebih aman, dan penanganan wilayah ambigu. Mengejar codec entropy baru belum menjadi hambatan utama.

## Referensi teknis primer

- PyMuPDF text extraction: https://pymupdf.readthedocs.io/en/latest/app1.html
- PyMuPDF Page API: https://pymupdf.readthedocs.io/en/latest/page.html
- Pillow image formats: https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html
- MessagePack: https://msgpack.org/

Referensi tersebut mendasari penggunaan API/format. Semua ukuran, runtime, dan temuan visual di atas berasal dari eksperimen ini, bukan benchmark vendor.
