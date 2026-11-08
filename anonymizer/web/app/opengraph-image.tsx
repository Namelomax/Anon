import { ImageResponse } from "next/og";

export const alt = "Anon — маскирование персональных данных в документах";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function OpenGraphImage() {
  return new ImageResponse(
    <div style={{ width: "100%", height: "100%", display: "flex", flexDirection: "column", justifyContent: "space-between", padding: "70px 80px", background: "#09111e", color: "#eef4ff" }}>
      <div style={{ display: "flex", fontSize: 60, color: "#86e8d6" }}>anon.</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
        <div style={{ display: "flex", fontSize: 54, lineHeight: 1.2 }}>[PERSON_1]</div>
        <div style={{ display: "flex", fontSize: 40, color: "#aab9cc" }}>[PHONE_1]  [EMAIL_1]</div>
      </div>
      <div style={{ display: "flex", fontSize: 28, color: "#86e8d6" }}>anonai.ru</div>
    </div>,
    size,
  );
}
