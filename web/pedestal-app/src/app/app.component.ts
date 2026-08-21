import { Component, OnDestroy, OnInit } from '@angular/core';
import { WebSocketService, GateEntryDTO } from './websocket.service';
import { ConfigService } from './config.service';
import { CommonModule } from '@angular/common';

// Sin heartbeat de lectores por más de esto, el indicador pasa a inactivo
// (el dispositivo manda uno cada 15 s; ver device/aditum_gate/screen.py)
const READER_STALE_MS = 60000;

// La pantalla nunca se queda pegada: si el dispositivo no reemplaza un
// estado transitorio dentro de estos lapsos, se muestra el aviso de fallo
// y se vuelve al reposo. Pasa cuando el backend autoriza pero nunca llama
// de vuelta al Pi (red caida, entry point mal configurado).
const LIMITE_MS: { [state: number]: number } = {
  6: 8000,   // ESCANEANDO (verify contra el backend + su callback)
  3: 90000,  // POR FAVOR ESPERE (el oficial autoriza a mano)
};

// Estado local (el dispositivo nunca lo manda) para el aviso de fallo
const ESTADO_FALLO = 7;
const FALLO_MS = 12000;

// De dia el reflejo del sol tapa el QR y hay que subir el brillo del
// celular; de noche un brillo alto satura al lector y hay que bajarlo.
const CR_HORA = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/Costa_Rica',
  hour: 'numeric',
  hourCycle: 'h23',
});
const DIA_DESDE = 6;
const DIA_HASTA = 18;

// El reloj muestra SIEMPRE la hora de Costa Rica (UTC-6), sin importar la
// zona horaria configurada en el equipo
const CR_TIME = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/Costa_Rica',
  hour: 'numeric',
  minute: '2-digit',
  second: '2-digit',
  hour12: true,
});

@Component({
  selector: 'app-root',
  templateUrl: './app.component.html',
  styleUrls: ['./app.component.css'],
  standalone: true,
  imports: [CommonModule]
})
export class AppComponent implements OnInit, OnDestroy {
  state: number = 1; // Estado inicial por defecto
  name: string = '';
  doorType: string = 'ENTRY';
  clientLogoUrl: string = '';

  // Pie: reloj e indicador del lector
  time: string = '--:--:--';
  ampm: string = '';
  connOk: boolean = true;
  connLabel: string = 'Lector QR activo';

  // Consejo de brillo del aviso de fallo (depende de la hora, ver CR_HORA)
  consejoBrillo: string = '';

  private wsConnected = true;
  private readerOk = true;
  private readerStatusAt = 0;
  private clockInterval?: ReturnType<typeof setInterval>;
  private stateTimer?: ReturnType<typeof setTimeout>;

  constructor(
    private webSocketService: WebSocketService,
    private configService: ConfigService,
  ) {}

  ngOnInit() {
    this.configService.config$.subscribe((config) => {
      this.doorType = config.screen.doorType ?? 'ENTRY';
      this.clientLogoUrl = config.screen.clientLogoUrl ?? '';
    });

    this.webSocketService.gateEntry$.subscribe((data: GateEntryDTO) => {
      this.setState(data.state ?? 1, data.name ?? '');
    });

    this.webSocketService.connected$.subscribe((connected) => {
      this.wsConnected = connected;
      this.updateConnStatus();
    });

    this.webSocketService.readerStatus$.subscribe((status) => {
      if (status) {
        this.readerOk = status.ok;
        this.readerStatusAt = status.at;
        this.updateConnStatus();
      }
    });

    this.tick();
    this.clockInterval = setInterval(() => this.tick(), 1000);
  }

  ngOnDestroy() {
    clearInterval(this.clockInterval);
    clearTimeout(this.stateTimer);
  }

  /** Cambia de estado y arma el temporizador que evita quedarse pegado. */
  private setState(state: number, name = '') {
    clearTimeout(this.stateTimer);
    this.stateTimer = undefined;
    this.state = state;
    this.name = name;
    if (state === ESTADO_FALLO) {
      this.consejoBrillo = this.consejoBrilloActual();
    }

    const limite = LIMITE_MS[state];
    if (limite) {
      this.stateTimer = setTimeout(() => this.setState(ESTADO_FALLO), limite);
    } else if (state === ESTADO_FALLO) {
      this.stateTimer = setTimeout(() => this.setState(1), FALLO_MS);
    }
  }

  /** Subir o bajar el brillo del celular segun sea de dia o de noche. */
  private consejoBrilloActual(): string {
    const hora = Number(CR_HORA.format(new Date()));
    return hora >= DIA_DESDE && hora < DIA_HASTA
      ? 'Suba el brillo del celular.'
      : 'Baje el brillo del celular.';
  }

  private tick() {
    const parts = CR_TIME.formatToParts(new Date());
    const get = (type: string) => parts.find((p) => p.type === type)?.value ?? '';
    this.time = `${get('hour')}:${get('minute')}:${get('second')}`;
    this.ampm = get('dayPeriod').toLowerCase();
    this.updateConnStatus();
  }

  private updateConnStatus() {
    const stale = this.readerStatusAt > 0 &&
      Date.now() - this.readerStatusAt > READER_STALE_MS;
    if (!navigator.onLine || !this.wsConnected) {
      this.connOk = false;
      this.connLabel = 'Sin conexión';
    } else if (!this.readerOk || stale) {
      this.connOk = false;
      this.connLabel = 'Lector QR no detectado';
    } else {
      this.connOk = true;
      this.connLabel = 'Lector QR activo';
    }
  }
}
