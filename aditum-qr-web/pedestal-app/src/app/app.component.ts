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
  message: string = 'Por favor coloque el código QR'; 
  clientLogoUrl: string = 'https://res.cloudinary.com/aditum/image/upload/v1501920877/fzncrputkdgm8iasuc3t.jpg';// Mensaje por defecto

  constructor(private webSocketService: WebSocketService) {}

  ngOnInit() {
    this.webSocketService.gateEntry$.subscribe((data: GateEntryDTO) => {
        console.log('GateEntryDTO received:', data);
        this.state = data.state ?? 1; // Establece el estado recibido o usa 1 por defecto
        this.name = data.name ?? '';

        // Actualiza el mensaje según el estado recibido
        switch (this.state) {
            case 1:
                this.message = 'Por favor coloque el código QR';
                break;
            case 2:
                this.message = `Código aceptado, bienvenido: ${this.name}`;
                break;
            case 3:
                this.message = 'Por favor espere a ser aceptado';
                break;
            case 4:
                this.message = 'Código no leído';
                break;
            case 5:
                this.message = `Hasta pronto`;
                break;
            default:
                this.message = 'Por favor coloque el código QR';
                break;
        }
    });
  }

  // Método para definir el color de fondo según el estado
  getBackgroundColor(state: number): string {
    switch (state) {
      case 1: return 'white';
      case 2: return 'green';
      case 3: return 'yellow';
      case 4: return 'red';
      case 5: return 'grey';
      default: return 'white';
    }
  }
}
