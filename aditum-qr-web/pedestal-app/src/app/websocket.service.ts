import { Injectable } from '@angular/core';
import { Subject } from 'rxjs';

export interface GateEntryDTO {
  name: string;
  isAutorized: boolean;
  isAutomatic: boolean;
  state?: number; // Agrega el campo estado si es necesario
}

@Injectable({
  providedIn: 'root'
})
export class WebSocketService {
  private socket: WebSocket;
  private gateEntrySubject = new Subject<GateEntryDTO>();

  gateEntry$ = this.gateEntrySubject.asObservable();

  constructor() {
    this.socket = new WebSocket('ws://localhost:3000');
    
    this.socket.onmessage = (event) => {
      console.log('Message received:', event.data);
      const data: GateEntryDTO = JSON.parse(event.data);
      this.gateEntrySubject.next(data);
    };

    this.socket.onopen = () => {
      console.log('WebSocket connection established');
    };
  
    this.socket.onclose = () => {
      console.log('WebSocket connection closed');
    };
  
    this.socket.onerror = (error) => {
      console.error('WebSocket error:', error);
    };
  }
}
