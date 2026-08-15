import { Injectable } from '@angular/core';
import { BehaviorSubject, Subject } from 'rxjs';

export interface GateEntryDTO {
  name: string;
  isAutorized: boolean;
  isAutomatic: boolean;
  state?: number;
}

export interface ReaderStatus {
  ok: boolean;
  at: number; // timestamp del último heartbeat recibido
}

const RECONNECT_DELAY_MS = 3000;

@Injectable({
  providedIn: 'root'
})
export class WebSocketService {
  private socket!: WebSocket;
  private gateEntrySubject = new Subject<GateEntryDTO>();
  private connectedSubject = new BehaviorSubject<boolean>(true);
  private readerStatusSubject = new BehaviorSubject<ReaderStatus | null>(null);

  gateEntry$ = this.gateEntrySubject.asObservable();
  /** Conexión al server local :3000 (si cae, no llegan los escaneos) */
  connected$ = this.connectedSubject.asObservable();
  /** Último heartbeat de lectores del dispositivo (null hasta el primero) */
  readerStatus$ = this.readerStatusSubject.asObservable();

  constructor() {
    this.connect();
  }

  private connect(): void {
    // La app se sirve desde el mismo server.js que expone el WebSocket
    this.socket = new WebSocket(`ws://${location.host}`);

    this.socket.onopen = () => {
      this.connectedSubject.next(true);
    };

    this.socket.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.state === 'reload') {
        // El config-agent aplicó una configuración nueva
        location.reload();
        return;
      }
      if (typeof data.readerOk === 'boolean') {
        // Heartbeat de lectores: no es un estado de pantalla
        this.readerStatusSubject.next({ ok: data.readerOk, at: Date.now() });
        return;
      }
      this.gateEntrySubject.next(data as GateEntryDTO);
    };

    this.socket.onclose = () => {
      console.warn('WebSocket cerrado; reintentando en 3s');
      this.connectedSubject.next(false);
      setTimeout(() => this.connect(), RECONNECT_DELAY_MS);
    };

    this.socket.onerror = (error) => {
      console.error('WebSocket error:', error);
      this.socket.close();
    };
  }
}
