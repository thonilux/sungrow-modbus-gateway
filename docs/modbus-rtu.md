# Sungrow MGRL - Komunikasi Modbus/RS485

Ekstraksi dari `User Manual MGRL Sungrow`.

> Catatan penting: manual ini tidak menyediakan tabel register Modbus RTU, format frame RTU, baud rate, alamat perangkat, function code, atau algoritma CRC. Manual hanya mendokumentasikan antarmuka komunikasi dan menyebut Modbus TCP/IP untuk komunikasi LAN.

## Ringkasan protokol yang disebut

| Antarmuka | Protokol / fungsi | Keterangan |
|---|---|---|
| WLAN | WiNet-S2 | Monitoring melalui iSolarCloud App atau iSolarCloud Web |
| Ethernet/LAN | Modbus TCP/IP | EMS atau logger pihak ketiga dapat mengontrol on/off, derating, charging, dan discharging inverter |
| RS485 | Komunikasi serial | Digunakan untuk koneksi monitoring; COM2 menghubungkan perangkat seperti CT dan baterai |
| CAN | Komunikasi baterai | Pada terminal BMS-485/CAN, manual menyebut pin CAN digunakan untuk baterai |

## COM2 dan terminal komunikasi

COM2 digunakan untuk komunikasi RS485 dengan perangkat seperti CT dan baterai.

### Label COM2

| Label | Pin / terminal | Fungsi menurut manual |
|---|---|---|
| DO | `NO/1`, `COM/2` | Alarm ground fault, intelligent load control, atau kontrol genset |
| Parallel_1 | `1-8` | Komunikasi paralel master-slave |
| Parallel_2 | `1-8` | Komunikasi paralel master-slave |
| DRM | `3V3`, `DRM0` | Reserved; ketersediaan perlu dikonfirmasi ke Sungrow |
| RSD | `RSD_1`, `RSD_2` | Reserved |
| BAT_TEM | `PT-`, `PT+` | Reserved |
| METER | `A2`, `B2` | Smart energy meter |
| CT | `CT-`, `CT+` | Sinyal sampling CT grid |
| BMS-485/CAN | `1/B`, `2/A`, `3`, `4/H`, `5/L`, `6`, `7/A`, `8/B` | Komunikasi baterai; hanya `4/H` dan `5/L` yang digunakan untuk CAN, sedangkan RS485 dinyatakan reserved |

## Koneksi RS485

- Antarmuka RS485 dipakai untuk membangun koneksi dengan perangkat monitoring.
- Jika jarak komunikasi `L <= 10 m`, gunakan kabel komunikasi RS485 secara langsung.
- Jika `L > 10 m`, tambahkan resistor `120 ohm` untuk meningkatkan kualitas komunikasi.
- Sebelum menghubungkan kabel baterai, pastikan urutan kabel sesuai.
- Untuk model baterai lain, ikuti manual baterai yang bersangkutan.

## Smart energy meter

Smart energy meter eksternal diperlukan untuk fungsi export control. Kabel meter dihubungkan ke port `METER` pada konektor plug-in, dengan terminal komunikasi `A2` dan `B2`.

Meter digunakan terutama untuk mendeteksi arah dan besarnya arus. Manual menyatakan bahwa data meter mungkin tidak digunakan untuk keperluan billing.

## Koneksi baterai

Terminal `BMS-485/CAN` berada pada COM2. Manual menyebut:

- `4/H` dan `5/L` digunakan untuk komunikasi baterai melalui protokol CAN.
- RS485 pada bagian BMS-485/CAN dinyatakan reserved.
- Konsultasikan manual baterai untuk koneksi di sisi baterai.

## Batas informasi Modbus RTU

Parameter berikut tidak ditemukan dalam `User Manual MGRL Sungrow`:

- baud rate dan parity RS485;
- alamat slave/device;
- function code Modbus RTU;
- format request/response;
- CRC-16;
- register address, tipe data, scaling, dan peta status;
- contoh frame Modbus RTU.

Untuk implementasi Modbus RTU inverter, diperlukan dokumen terpisah seperti protocol/register map resmi Sungrow untuk model dan versi firmware yang tepat. Jangan memakai informasi Modbus TCP/IP pada manual ini sebagai pengganti register map RTU tanpa konfirmasi dari Sungrow.

## Referensi lokasi di manual

- Communication and Configuration: halaman manual 19
- Label COM2 dan BMS-485/CAN: halaman manual 37
- Communication Connection: halaman manual 58
- COM2 Terminal Communication Connection: halaman manual 61
- Smart energy meter dan terminasi RS485: halaman manual 64-65
- Battery Communication Connection: halaman manual 65-66
