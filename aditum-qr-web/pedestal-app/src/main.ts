import { bootstrapApplication } from '@angular/platform-browser';
import { AppComponent } from './app/app.component';
import { HttpClientModule } from '@angular/common/http';
import { provideHttpClient } from '@angular/common/http'; // Import provideHttpClient for standalone components

bootstrapApplication(AppComponent, {
  providers: [
    provideHttpClient()  // Ensure HttpClientModule is provided
  ]
})
.catch(err => console.error(err));
