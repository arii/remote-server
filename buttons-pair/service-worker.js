import { createPairing } from "./pairing.js";

const pairing = createPairing(chrome, WebSocket);
void pairing.start();
