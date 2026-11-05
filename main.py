from kivy.config import Config

# E-ink friendly defaults (Onyx Boox Max Lumi): lower redraw rate and disable multisampling
# to reduce ghosting and improve responsiveness/battery.
Config.set('graphics', 'maxfps', '20')
Config.set('graphics', 'multisamples', '0')

from pdf_compressor_kivy_app import PdfCompressorApp


if __name__ == '__main__':
    PdfCompressorApp().run()
