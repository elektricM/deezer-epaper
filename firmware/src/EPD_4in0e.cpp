/*****************************************************************************
* | File        :   EPD_4in0e.c
* | Author      :   Waveshare team
* | Function    :   4inch e-Paper (E) Driver
* | Info        :
*----------------
* | This version:   V1.0
* | Date        :   2024-08-20
* | Info        :
* -----------------------------------------------------------------------------
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
#include "EPD_4in0e.h"
#include "Debug.h"

/******************************************************************************
function :  Software reset
parameter:
******************************************************************************/
static void EPD_4IN0E_Reset(void)
{
    DEV_Digital_Write(EPD_RST_PIN, 1);
    DEV_Delay_ms(20);
    DEV_Digital_Write(EPD_RST_PIN, 0);
    DEV_Delay_ms(2);
    DEV_Digital_Write(EPD_RST_PIN, 1);
    DEV_Delay_ms(20);
}

/******************************************************************************
function :  send command
parameter:
     Reg : Command register
******************************************************************************/
static void EPD_4IN0E_SendCommand(UBYTE Reg)
{
    DEV_Digital_Write(EPD_DC_PIN, 0);
    DEV_Digital_Write(EPD_CS_PIN, 0);
    DEV_SPI_WriteByte(Reg);
    DEV_Digital_Write(EPD_CS_PIN, 1);
}

/******************************************************************************
function :  send data
parameter:
    Data : Write data
******************************************************************************/
static void EPD_4IN0E_SendData(UBYTE Data)
{
    DEV_Digital_Write(EPD_DC_PIN, 1);
    DEV_Digital_Write(EPD_CS_PIN, 0);
    DEV_SPI_WriteByte(Data);
    DEV_Digital_Write(EPD_CS_PIN, 1);
}

/******************************************************************************
function :  Wait until the busy_pin goes LOW
parameter:
******************************************************************************/
// Timeout in ms. A refresh takes ~22 s; anything past 40 s means the panel is
// not going to answer.
#define EPD_4IN0E_BUSY_TIMEOUT_MS 40000

// Set when the busy wait gave up. Callers can report it; the panel has already
// been powered down by then.
bool EPD_4IN0E_timed_out = false;

/******************************************************************************
function :  Wait for BUSY to go high, but NOT forever.

The vendor version spins unbounded. BUSY (GPIO25) is an input with no pull, so a
disconnected or faulty ribbon floats low and that loop never exits - with the
DC/DC converter on and high voltage sitting on the glass, because the two calls
that matter are immediately after POWER_ON and after DISPLAY_REFRESH. Sleep() is
then never reached. Leaving the panel energised is the one failure Waveshare
describe as unrepairable, so on timeout we power off and deep sleep rather than
hang holding voltage on the panel.
******************************************************************************/
static bool EPD_4IN0E_ReadBusyH(void)
{
    Debug("e-Paper busy H\r\n");
    unsigned long start = millis();
    while(!DEV_Digital_Read(EPD_BUSY_PIN)) {      //LOW: busy, HIGH: idle
        if (millis() - start > EPD_4IN0E_BUSY_TIMEOUT_MS) {
            EPD_4IN0E_timed_out = true;
            EPD_4IN0E_SendCommand(0x02);   // POWER_OFF
            EPD_4IN0E_SendData(0x00);
            EPD_4IN0E_SendCommand(0x07);   // DEEP_SLEEP
            EPD_4IN0E_SendData(0xA5);
            return false;
        }
        DEV_Delay_ms(10);
    }
    DEV_Delay_ms(200);
    Debug("e-Paper busy H release\r\n");
    return true;
}

/******************************************************************************
function :  Turn On Display
parameter:
******************************************************************************/
static void EPD_4IN0E_TurnOnDisplay(void)
{
    
    EPD_4IN0E_SendCommand(0x04); // POWER_ON
    EPD_4IN0E_ReadBusyH();
    DEV_Delay_ms(200);

    //Second setting 
    EPD_4IN0E_SendCommand(0x06);
    EPD_4IN0E_SendData(0x6F);
    EPD_4IN0E_SendData(0x1F);
    EPD_4IN0E_SendData(0x17);
    EPD_4IN0E_SendData(0x27);
    DEV_Delay_ms(200);

    EPD_4IN0E_SendCommand(0x12); // DISPLAY_REFRESH
    EPD_4IN0E_SendData(0x00);
    EPD_4IN0E_ReadBusyH();

    EPD_4IN0E_SendCommand(0x02); // POWER_OFF
    EPD_4IN0E_SendData(0X00);
    EPD_4IN0E_ReadBusyH();
    DEV_Delay_ms(200);
}

/******************************************************************************
function :  Initialize the e-Paper register
parameter:
******************************************************************************/
void EPD_4IN0E_Init(void)
{
    EPD_4IN0E_Reset();
    EPD_4IN0E_ReadBusyH();
    DEV_Delay_ms(30);

    EPD_4IN0E_SendCommand(0xAA);    // CMDH
    EPD_4IN0E_SendData(0x49);
    EPD_4IN0E_SendData(0x55);
    EPD_4IN0E_SendData(0x20);
    EPD_4IN0E_SendData(0x08);
    EPD_4IN0E_SendData(0x09);
    EPD_4IN0E_SendData(0x18);

    EPD_4IN0E_SendCommand(0x01);
    EPD_4IN0E_SendData(0x3F);

    EPD_4IN0E_SendCommand(0x00);
    EPD_4IN0E_SendData(0x5F);
    EPD_4IN0E_SendData(0x69);

    EPD_4IN0E_SendCommand(0x05);
    EPD_4IN0E_SendData(0x40);
    EPD_4IN0E_SendData(0x1F);
    EPD_4IN0E_SendData(0x1F);
    EPD_4IN0E_SendData(0x2C);

    EPD_4IN0E_SendCommand(0x08);
    EPD_4IN0E_SendData(0x6F);
    EPD_4IN0E_SendData(0x1F);
    EPD_4IN0E_SendData(0x1F);
    EPD_4IN0E_SendData(0x22);

    EPD_4IN0E_SendCommand(0x06);
    EPD_4IN0E_SendData(0x6F);
    EPD_4IN0E_SendData(0x1F);
    EPD_4IN0E_SendData(0x17);
    EPD_4IN0E_SendData(0x17);

    EPD_4IN0E_SendCommand(0x03);
    EPD_4IN0E_SendData(0x00);
    EPD_4IN0E_SendData(0x54);
    EPD_4IN0E_SendData(0x00);
    EPD_4IN0E_SendData(0x44); 

    EPD_4IN0E_SendCommand(0x60);
    EPD_4IN0E_SendData(0x02);
    EPD_4IN0E_SendData(0x00);

    EPD_4IN0E_SendCommand(0x30);
    EPD_4IN0E_SendData(0x08);

    EPD_4IN0E_SendCommand(0x50);
    EPD_4IN0E_SendData(0x3F);

    EPD_4IN0E_SendCommand(0x61);
    EPD_4IN0E_SendData(0x01);
    EPD_4IN0E_SendData(0x90);
    EPD_4IN0E_SendData(0x02); 
    EPD_4IN0E_SendData(0x58);

    EPD_4IN0E_SendCommand(0xE3);
    EPD_4IN0E_SendData(0x2F);

    EPD_4IN0E_SendCommand(0x84);
    EPD_4IN0E_SendData(0x01);
    EPD_4IN0E_ReadBusyH();

}

/******************************************************************************
function :  Clear screen
parameter:
******************************************************************************/
void EPD_4IN0E_Clear(UBYTE color)
{
    UWORD Width, Height;
    Width = (EPD_4IN0E_WIDTH % 2 == 0)? (EPD_4IN0E_WIDTH / 2 ): (EPD_4IN0E_WIDTH / 2 + 1);
    Height = EPD_4IN0E_HEIGHT;

    EPD_4IN0E_SendCommand(0x10);
    for (UWORD j = 0; j < Height; j++) {
        for (UWORD i = 0; i < Width; i++) {
            EPD_4IN0E_SendData((color<<4)|color);
        }
    }

    EPD_4IN0E_TurnOnDisplay();
}

/******************************************************************************
function :  show 7 kind of color block
parameter:
******************************************************************************/
void EPD_4IN0E_Show7Block(void)
{
    unsigned long j, k;
    unsigned char const Color_seven[6] = 
    {EPD_4IN0E_BLACK, EPD_4IN0E_YELLOW, EPD_4IN0E_RED, EPD_4IN0E_BLUE, EPD_4IN0E_GREEN, EPD_4IN0E_WHITE};

    EPD_4IN0E_SendCommand(0x10);
    for(k = 0 ; k < 6; k ++) {
        for(j = 0 ; j < 20000; j ++) {
            EPD_4IN0E_SendData((Color_seven[k]<<4) |Color_seven[k]);
        }
    }
    EPD_4IN0E_TurnOnDisplay();
}

void EPD_4IN0E_Show(void)
{
    unsigned long k,o;
    unsigned char const Color_seven[6] = 
    {EPD_4IN0E_BLACK, EPD_4IN0E_YELLOW, EPD_4IN0E_RED, EPD_4IN0E_BLUE, EPD_4IN0E_GREEN, EPD_4IN0E_WHITE};

    UWORD Width, Height;
    Width = (EPD_4IN0E_WIDTH % 2 == 0)? (EPD_4IN0E_WIDTH / 2 ): (EPD_4IN0E_WIDTH / 2 + 1);
    Height = EPD_4IN0E_HEIGHT;
    k = 0;
    o = 0;

    EPD_4IN0E_SendCommand(0x10);
    for (UWORD j = 0; j < Height; j++) {
        if((j > 10) && (j<50))
        for (UWORD i = 0; i < Width; i++) {
                EPD_4IN0E_SendData((Color_seven[0]<<4) |Color_seven[0]);
            }
        else if(o < Height/2)
        for (UWORD i = 0; i < Width; i++) {
                EPD_4IN0E_SendData((Color_seven[0]<<4) |Color_seven[0]);
            }
        
        else
        {
            for (UWORD i = 0; i < Width; i++) {
                EPD_4IN0E_SendData((Color_seven[k]<<4) |Color_seven[k]);
                
            }
            k++ ;
            if(k >= 6)
                k = 0;
        }
            
        o++ ;
        if(o >= Height)
            o = 0;
    }
    EPD_4IN0E_TurnOnDisplay();
}

/******************************************************************************
function :  Sends the image buffer in RAM to e-Paper and displays
parameter:
******************************************************************************/
void EPD_4IN0E_Display(const UBYTE *Image)
{
    UWORD Width, Height;
    Width = (EPD_4IN0E_WIDTH % 2 == 0)? (EPD_4IN0E_WIDTH / 2 ): (EPD_4IN0E_WIDTH / 2 + 1);
    Height = EPD_4IN0E_HEIGHT;

    EPD_4IN0E_SendCommand(0x10);
    for (UWORD j = 0; j < Height; j++) {
        for (UWORD i = 0; i < Width; i++) {
            EPD_4IN0E_SendData(Image[i + j * Width]);
        }
    }
    EPD_4IN0E_TurnOnDisplay();
}

/******************************************************************************
function :  Streamed display - load the frame in pieces, never all at once
parameter:
    This exists because a whole frame does not fit in RAM. 400x600 at two pixels
    per byte is 120,000 bytes, and on an ESP32-D0WDQ6 the largest CONTIGUOUS
    free block at boot is about 110,580 - measured, after allocating before
    anything else touches the heap. Free heap is not the constraint,
    fragmentation is, so no amount of allocating early would have worked.

    The panel does not need the frame in memory anyway: EPD_4IN0E_Display just
    sends command 0x10 followed by every byte in order. So the bytes can go
    straight from the network to the controller as they arrive.

    Nothing appears on the glass until Finish() sends the refresh command, so an
    interrupted transfer is harmless - it leaves the controller's RAM partly
    written and simply never triggers a refresh.
******************************************************************************/
void EPD_4IN0E_DisplayBegin(void)
{
    EPD_4IN0E_SendCommand(0x10);
}

void EPD_4IN0E_DisplayFeed(const UBYTE *chunk, UDOUBLE len)
{
    // Bulk write over the SPI peripheral. Byte-at-a-time bit-banging cost 4.7 s
    // per frame, which was 19% of the whole time between pressing skip and
    // seeing the new cover.
    DEV_SPI_Write_Bulk(chunk, len);
}

void EPD_4IN0E_DisplayFinish(void)
{
    EPD_4IN0E_TurnOnDisplay();
}

void EPD_4IN0E_DisplayPart(const UBYTE *Image, UWORD xstart, UWORD ystart, UWORD image_width, UWORD image_heigh)
{
	unsigned long i, j;
	UWORD Width, Height;
	Width = (EPD_4IN0E_WIDTH % 2 == 0)? (EPD_4IN0E_WIDTH / 2 ): (EPD_4IN0E_WIDTH / 2 + 1);
	Height = EPD_4IN0E_HEIGHT;
	
	EPD_4IN0E_SendCommand(0x10);
	for(i=0; i<Height; i++) {
		for(j=0; j<Width; j++) {
			if((i<(image_heigh+ystart)) && (i>=ystart) && (j<((image_width+xstart)/2)) && (j>=(xstart/2))) {
				EPD_4IN0E_SendData(Image[(j-xstart/2) + (image_width/2*(i-ystart))]);
			}
			else {
				EPD_4IN0E_SendData(0x11);
			}
		}
	}
	EPD_4IN0E_TurnOnDisplay();
}

/******************************************************************************
function :  Enter sleep mode
parameter:
******************************************************************************/
void EPD_4IN0E_Sleep(void)
{
    EPD_4IN0E_SendCommand(0x07); // DEEP_SLEEP
    EPD_4IN0E_SendData(0XA5);
    // EPD_4IN0E_ReadBusyH();
}

/******************************************************************************
function :  Set the partial window (command 0x83)
parameter:  x, y, w, h - window in pixels; x and w must be even (2 px per byte)

0x83 is undocumented by Waveshare and absent from their driver. Good Display
found it and GxEPD2 1.6.8 uses it for the 7.3" Spectra 6 (GDEP073E01). It is
NOT known to work on this 4" panel - that is what we are testing. Note that
GxEPD2's author reports some of his 6/7-colour panels do not decode it.

Byte layout copied from GxEPD2_730c_GDEP073E01::_setPartialRamArea, including
the y-end quirk: ye is y + h, one more than you would expect.
******************************************************************************/
void EPD_4IN0E_SetPartialWindow(UWORD x, UWORD y, UWORD w, UWORD h)
{
    UWORD xe = x + w - 1;
    UWORD ye = y + h;   // controller quirk: one more, per GxEPD2
    EPD_4IN0E_SendCommand(0x83);
    EPD_4IN0E_SendData(x / 256);
    EPD_4IN0E_SendData(x % 256);
    EPD_4IN0E_SendData(xe / 256);
    EPD_4IN0E_SendData(xe % 256);
    EPD_4IN0E_SendData(y / 256);
    EPD_4IN0E_SendData(y % 256);
    EPD_4IN0E_SendData(ye / 256);
    EPD_4IN0E_SendData(ye % 256);
    EPD_4IN0E_SendData(0x01);
}

/******************************************************************************
function :  Partial-window experiment - writes a full frame, then overwrites a
            sub-rectangle through the 0x83 window, then refreshes once.
parameter:  full - 400x600 image; rect - w x h image; x, y, w, h - window

Reading the result:
  block lands in the window   -> 0x83 works, area outside is preserved
  block lands at the top-left -> 0x83 ignored, the second write restarted at 0,0
This costs exactly one refresh either way.
******************************************************************************/
void EPD_4IN0E_DisplayPartialTest(const UBYTE *full, const UBYTE *rect,
                                  UWORD x, UWORD y, UWORD w, UWORD h,
                                  UBYTE border)
{
    UWORD full_stride = EPD_4IN0E_WIDTH / 2;
    UWORD rect_stride = w / 2;
    unsigned long i, j;

    // 1. full frame into controller RAM
    EPD_4IN0E_SendCommand(0x10);
    for (i = 0; i < EPD_4IN0E_HEIGHT; i++)
        for (j = 0; j < full_stride; j++)
            EPD_4IN0E_SendData(full[j + full_stride * i]);

    // 2. narrow the window, then write only the block
    EPD_4IN0E_SetPartialWindow(x, y, w, h);
    EPD_4IN0E_SendCommand(0x10);
    for (i = 0; i < h; i++)
        for (j = 0; j < rect_stride; j++)
            EPD_4IN0E_SendData(rect[j + rect_stride * i]);

    // 3. refresh; 0x50 = 0xFF is the floating border GxEPD2 uses for partial
    //    (Waveshare's init sets 0x50 = 0x3F for a full refresh)
    EPD_4IN0E_SendCommand(0x04); // POWER_ON
    EPD_4IN0E_ReadBusyH();
    DEV_Delay_ms(200);

    EPD_4IN0E_SendCommand(0x06);
    EPD_4IN0E_SendData(0x6F);
    EPD_4IN0E_SendData(0x1F);
    EPD_4IN0E_SendData(0x17);
    EPD_4IN0E_SendData(0x27);
    DEV_Delay_ms(200);

    // 0x50 is VCOM AND DATA INTERVAL SETTING; bits 7:6 are VBD, the border data
    // selection. 0x3F (Waveshare's init value) drives the border to a defined
    // state. 0xFF leaves it floating, which is what GxEPD2 uses for partial
    // updates - but a floating border accumulates charge and drifts into a
    // mottled mix of pigments. The border is a physical region outside the
    // active area (see BORDER DEFINITION, p.22 of the panel manual), so this is
    // cosmetic, but it looks bad. Pass 0x3F unless you are deliberately testing.
    EPD_4IN0E_SendCommand(0x50);
    EPD_4IN0E_SendData(border);

    EPD_4IN0E_SendCommand(0x12); // DISPLAY_REFRESH
    EPD_4IN0E_SendData(0x00);
    EPD_4IN0E_ReadBusyH();

    EPD_4IN0E_SendCommand(0x02); // POWER_OFF
    EPD_4IN0E_SendData(0x00);
    EPD_4IN0E_ReadBusyH();
    DEV_Delay_ms(200);
}

