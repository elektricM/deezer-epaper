/*****************************************************************************
* | File      	:   DEV_Config.c
* | Author      :   Waveshare team
* | Function    :   Hardware underlying interface
* | Info        :
*----------------
* |	This version:   V1.0
* | Date        :   2020-02-19
* | Info        :
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documnetation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to  whom the Software is
# furished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS OR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#
******************************************************************************/
#include "DEV_Config.h"
#include <SPI.h>

// Defined before first use: DEV_Module_Init() configures it.
SPIClass epdSPI(HSPI);

void GPIO_Config(void)
{
    
    

    pinMode(EPD_BUSY_PIN,  INPUT);
    pinMode(EPD_RST_PIN , OUTPUT);
    pinMode(EPD_DC_PIN  , OUTPUT);
    pinMode(EPD_PWR_PIN,  OUTPUT);

    pinMode(EPD_SCK_PIN, OUTPUT);
    pinMode(EPD_MOSI_PIN, OUTPUT);
    pinMode(EPD_CS_PIN , OUTPUT);
    //pinMode(EPD_CS_S_PIN , OUTPUT);

    digitalWrite(EPD_CS_PIN , HIGH);
    //digitalWrite(EPD_CS_S_PIN , HIGH);
    digitalWrite(EPD_SCK_PIN, LOW);
    digitalWrite(EPD_PWR_PIN , HIGH);
}

void GPIO_Mode(UWORD GPIO_Pin, UWORD Mode)
{
    if(Mode == 0) {
        pinMode(GPIO_Pin , INPUT);
	} else {
		pinMode(GPIO_Pin , OUTPUT);
	}
}
/******************************************************************************
function:	Module Initialize, the BCM2835 library and initialize the pins, SPI protocol
parameter:
Info:
******************************************************************************/
UBYTE DEV_Module_Init(void)
{
	//gpio
	GPIO_Config();

	//serial printf
	Serial.begin(115200);

	// Hardware SPI, in addition to the vendor's bit-banged path.
	//
	// The stock driver clocks every bit with digitalWrite(): 8 iterations per
	// byte, three calls each. For one 120,000-byte frame that is 2.88 million
	// digitalWrite calls, and on ESP32 each costs well over a microsecond -
	// measured at 4.7 s to load a single frame, against 20.5 s for the refresh
	// itself. The same bytes over the SPI peripheral at 20 MHz take about a
	// tenth of a second.
	//
	// Commands and one-off data still go through the bit-banged path, which is
	// known to work on this panel; only the pixel burst uses this.
	// This MUST run after GPIO_Config() and on EVERY init, not once.
	//
	// GPIO_Config() calls pinMode(SCK, OUTPUT) and pinMode(MOSI, OUTPUT), which
	// hands those pins back to the GPIO matrix and detaches them from the SPI
	// peripheral. Guarding this with a "already initialised" flag meant the
	// pins were re-attached exactly once: the first refresh after a boot drew
	// correctly and every one after it silently wrote into the void. The
	// symptom was a refresh that "finished" in 1200 ms instead of 20500 - the
	// panel had received no data, so there was nothing to wait for.
	//
	// SS is -1 on purpose: the driver drives CS itself, and letting the
	// peripheral claim it would stop DEV_Digital_Write touching the pin.
	epdSPI.end();
	epdSPI.begin(EPD_SCK_PIN, -1 /* no MISO */, EPD_MOSI_PIN, -1 /* no SS */);
	epdSPI.setFrequency(20000000);
	epdSPI.setDataMode(SPI_MODE0);
	epdSPI.setBitOrder(MSBFIRST);

	return 0;
}

/******************************************************************************
function:
			SPI read and write
******************************************************************************/


/// Bulk pixel write over the SPI peripheral, chip select held low for the whole
/// burst rather than toggled per byte. The controller accepts a continuous
/// stream after command 0x10; toggling CS 120,000 times was pure overhead.
void DEV_SPI_Write_Bulk(const UBYTE *buf, UDOUBLE len)
{
    DEV_Digital_Write(EPD_DC_PIN, 1);
    DEV_Digital_Write(EPD_CS_PIN, 0);
    epdSPI.writeBytes(buf, len);
    DEV_Digital_Write(EPD_CS_PIN, 1);
}

void DEV_SPI_WriteByte(UBYTE data)
{
    // Everything goes through the peripheral now. The two paths cannot coexist:
    // once epdSPI.begin() claims SCK and MOSI, digitalWrite on them fails with
    // "IO is not set as GPIO" and nothing reaches the panel at all.
    epdSPI.transfer(data);
}

static void DEV_SPI_WriteByte_BitBang(UBYTE data)
{
    for (int i = 0; i < 8; i++)
    {
        if ((data & 0x80) == 0) digitalWrite(EPD_MOSI_PIN, GPIO_PIN_RESET); 
        else                    digitalWrite(EPD_MOSI_PIN, GPIO_PIN_SET);

        data <<= 1;
        digitalWrite(EPD_SCK_PIN, GPIO_PIN_SET);     
        digitalWrite(EPD_SCK_PIN, GPIO_PIN_RESET);
    }

}

UBYTE DEV_SPI_ReadByte()
{
    UBYTE j=0xff;
    GPIO_Mode(EPD_MOSI_PIN, 0);
    for (int i = 0; i < 8; i++)
    {
        j = j << 1;
        if (digitalRead(EPD_MOSI_PIN))  j = j | 0x01;
        else                            j = j & 0xfe;
        
        digitalWrite(EPD_SCK_PIN, GPIO_PIN_SET);     
        digitalWrite(EPD_SCK_PIN, GPIO_PIN_RESET);
    }
    GPIO_Mode(EPD_MOSI_PIN, 1);
    return j;
}

void DEV_SPI_Write_nByte(UBYTE *pData, UDOUBLE len)
{
    for (int i = 0; i < len; i++)
        DEV_SPI_WriteByte(pData[i]);
}


void DEV_Module_Exit(void)
{
    digitalWrite(EPD_PWR_PIN , LOW);
    digitalWrite(EPD_RST_PIN , LOW);
}
