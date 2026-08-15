import { Component, OnDestroy, OnInit } from '@angular/core';
import { WebSocketService, GateEntryDTO } from './websocket.service';
import { ConfigService } from './config.service';
import { CommonModule } from '@angular/common';

// Sin heartbeat de lectores por más de esto, el indicador pasa a inactivo
// (el dispositivo manda uno cada 15 s; ver device/aditum_gate/screen.py)
const READER_STALE_MS = 60000;

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

  private wsConnected = true;
  private readerOk = true;
  private readerStatusAt = 0;
  private clockInterval?: ReturnType<typeof setInterval>;

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
      this.state = data.state ?? 1;
      this.name = data.name ?? '';
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
