/* Original mascot family for the six agents.
 *
 * One silhouette shared by all six - a rounded body with a domed antenna and
 * two eyes - differentiated only by the prop each carries. Flat geometry, Ghost
 * Cyan on near-black, no gradients or illustration detail, so they stay legible
 * at 28px in the capability grid and at 40px beside a report section.
 *
 * Drawn from primitives rather than shipped as asset files: the shapes are
 * simple enough that the code is shorter than the SVG would be, and the family
 * stays consistent because every mascot reuses the same body() call. */

const MASCOT_INK = '#7FFFD4';

function body(prop) {
  return `
    <path d="M8 20a12 12 0 0 1 24 0v11a5 5 0 0 1-5 5H13a5 5 0 0 1-5-5z"
          fill="none" stroke="currentColor" stroke-width="1.6"/>
    <circle cx="15.5" cy="19" r="1.9" fill="currentColor"/>
    <circle cx="24.5" cy="19" r="1.9" fill="currentColor"/>
    <path d="M20 8V5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
    <circle cx="20" cy="3.6" r="1.6" fill="currentColor"/>
    <g transform="translate(20 29) scale(1.5) translate(-20 -29)"
       stroke-width="1.05" vector-effect="non-scaling-stroke">${prop}</g>
  `;
}

const PROPS = {
  magnifier: `
    <circle cx="18.5" cy="27.5" r="4.2" fill="none" stroke="currentColor" stroke-width="1.6"/>
    <path d="M21.6 30.6L25 34" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>`,

  shield: `
    <path d="M20 23l5 1.8v3.4c0 2.6-2 4.8-5 5.8-3-1-5-3.2-5-5.8v-3.4z"
          fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
    <path d="M20 26.6v2.6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    <circle cx="20" cy="31.2" r="0.9" fill="currentColor"/>`,

  ledger: `
    <path d="M20 25.4c-1.6-1.1-3.4-1.5-5.4-1.3v7.6c2-.2 3.8.2 5.4 1.3
             1.6-1.1 3.4-1.5 5.4-1.3v-7.6c-2-.2-3.8.2-5.4 1.3z"
          fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>
    <path d="M20 25.4V33" stroke="currentColor" stroke-width="1.3"/>`,

  envelope: `
    <rect x="14" y="24.5" width="12" height="8.4" rx="1.2"
          fill="none" stroke="currentColor" stroke-width="1.5"/>
    <path d="M14.4 25.4L20 29.4l5.6-4" fill="none"
          stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>`,

  flashlight: `
    <rect x="14.2" y="26.4" width="6.4" height="4.6" rx="1.1"
          fill="none" stroke="currentColor" stroke-width="1.5"/>
    <path d="M21.2 27.2l4.6-1.6v7.4l-4.6-1.6z" fill="none"
          stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>`,

  lock: `
    <rect x="15.4" y="27.4" width="9.2" height="6.4" rx="1.4"
          fill="none" stroke="currentColor" stroke-width="1.5"/>
    <path d="M17.6 27.4v-2a2.4 2.4 0 0 1 4.8 0v2" fill="none"
          stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>`,

  scales: `
    <path d="M20 24.4v8.8M15.4 27h9.2" stroke="currentColor"
          stroke-width="1.5" stroke-linecap="round"/>
    <path d="M13.4 30.2h4M22.6 30.2h4" stroke="currentColor"
          stroke-width="1.4" stroke-linecap="round"/>`,
};

/** Inline SVG for one agent's mascot. */
function mascot(prop, size = 40) {
  const shape = PROPS[prop] || PROPS.magnifier;
  return `<svg class="mascot" width="${size}" height="${size}" viewBox="0 0 40 40"
               role="img" aria-hidden="true" style="color:${MASCOT_INK}">${body(shape)}</svg>`;
}

window.WitnessMascots = { mascot, PROPS };
