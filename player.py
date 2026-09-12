import mido
import time

mido.set_backend('mido.backends.pygame')

port_name = "VirtualMIDISynth #1"
port = mido.open_output(port_name)

port.send(mido.Message('note_on', note=60, velocity=100))
time.sleep(1)
port.send(mido.Message('note_off', note=60))

port.close()