// Edit-visible HMR marker: editing the text below must hot-update in the
// browser without a full reload (proves HMR is working).
export const HMR_MARKER_TEXT = 'HMR marker v1 — edit me to verify hot update'

export default function HmrMarker() {
  return <p data-testid="hmr-marker">{HMR_MARKER_TEXT}</p>
}
