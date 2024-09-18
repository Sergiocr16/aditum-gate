import time
import board
import neopixel

# Initialize the NeoPixel strip
# GPIO Data Pin: D18, Number of LEDs: 55, Brightness: 1
pixels = neopixel.NeoPixel(board.D18, 55, brightness=1, auto_write=False)

# Function to set all LEDs to a specific color
def set_color(color):
    pixels.fill(color)
    pixels.show()

# Function to set all LEDs to white
def set_white():
    set_color((255, 255, 255))

# Function to turn LEDs green for 3 seconds
def green_for_3_seconds():
    set_color((0, 255, 0))
    time.sleep(3)
    set_white()

# Function to turn LEDs red for 3 seconds
def red_for_3_seconds():
    set_color((255, 0, 0))
    time.sleep(3)
    set_white()

# Start with white color
set_white()

# Example usage: Call these functions as needed
# green_for_3_seconds()
# red_for_3_seconds()
