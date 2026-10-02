// QR code for the authenticator enrolment URI, drawn as plain SVG rects (no innerHTML).
import qrcode from "qrcode-generator";

/** The dark module positions of `text` as a square matrix of booleans. */
export function qrMatrix(text) {
  const qr = qrcode(0, "M");
  qr.addData(text);
  qr.make();
  const size = qr.getModuleCount();
  return Array.from({ length: size }, (_, r) => Array.from({ length: size }, (_, c) => qr.isDark(r, c)));
}

export function QrCode({ text, label = "QR code" }) {
  const matrix = qrMatrix(text);
  const quiet = 2;
  const size = matrix.length + quiet * 2;
  return (
    <svg
      className="qr-code"
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label={label}
      shapeRendering="crispEdges"
    >
      <rect width={size} height={size} fill="#ffffff" />
      {matrix.flatMap((row, r) =>
        row.map((dark, c) =>
          dark ? <rect key={`${r}-${c}`} x={c + quiet} y={r + quiet} width="1" height="1" fill="#000000" /> : null,
        ),
      )}
    </svg>
  );
}
