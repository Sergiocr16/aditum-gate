import { Component, OnInit } from '@angular/core';
import { WebSocketService, GateEntryDTO } from './websocket.service'; 
import { CommonModule } from '@angular/common';

@Component({
  selector: 'app-root',
  templateUrl: './app.component.html',
  styleUrls: ['./app.component.css'],
  standalone: true,
  imports: [CommonModule]
})
export class AppComponent implements OnInit {
  state: number = 1; // Estado inicial por defecto
  name: string = '';
  clientLogoUrl: string = 'https://res.cloudinary.com/aditum/image/upload/v1501920877/fzncrputkdgm8iasuc3t.jpg';// Mensaje por defecto

  constructor(private webSocketService: WebSocketService) {}

  ngOnInit() {
    this.webSocketService.gateEntry$.subscribe((data: GateEntryDTO) => {
        console.log('GateEntryDTO received:', data);
        this.state = data.state ?? 1; // Establece el estado recibido o usa 1 por defecto
        this.name = data.name ?? '';
    });
  }
}
