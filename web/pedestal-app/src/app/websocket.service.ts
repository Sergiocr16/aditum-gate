import { Injectable } from '@angular/core';
import { Subject } from 'rxjs';

export interface GateEntryDTO {
  name: string;
  isAutorized: boolean;
  isAutomatic: boolean;
  state?: number;
}

const RECONNECT_DELAY_MS = 3000;

@Injectable({
  providedIn: 'root'
})
export class WebSocketService {
  private socket!: WebSocket;
  private gateEntrySubject = new Subject<GateEntryDTO>();

  gateEntry$ = this.gateEntrySubject.asObservable();

  constructor() {
    this.connect();
  }

  private connect(): void {
    // La app se sirve desde el mismo server.js que expone el WebSocket
    this.socket = new WebSocket(`ws://${location.host}`);

    this.socket.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.state === 'reload') {
        // El config-agent aplicó una configuración nueva
        location.reload();
        return;
      }
      this.gateEntrySubject.next(data as GateEntryDTO);
    };

    this.socket.onclose = () => {
      console.warn('WebSocket cerrado; reintentando en 3s');
      setTimeout(() => this.connect(), RECONNECT_DELAY_MS);
    };

    this.socket.onerror = (error) => {
      console.error('WebSocket error:', error);
      this.socket.close();
    };
  }
}
