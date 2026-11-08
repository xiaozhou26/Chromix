"""Independent reconstructed155 script bytes and version-isolated generator checks."""
import base64
import hashlib
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zlib

from tools import linux_restored_generators as generators

# DevTools cd7bfad9 + portablelinux 025d99db patches + core ffeb45a2 domains.
SCRIPTS = json.loads(zlib.decompress(base64.b85decode(
    b"c-rlKYkS+avhH6&owSk?OKKeV<~~_xC3e!LJ579|ba$^4`^XdtNlYk$iy&=lrT*`G1{V^%=w^Xj`@`1LIuZ%IGZ+j8GXoI(a=_i#"
    b"j}ktdq<-KHDHs0^KJkMSaxff{Qx+{_e>P9Z(YHrO<inhj(|ODme!3vf(`3$Kei1^sKR6|9N|L$H31?~SQsOd?68Je|*E9~PM<z?+"
    b"kQbw~K2MebC4ukK5UM3}Cn2sAk_i=1Q<jDv@k6*l$(z@wFW-({lBpli!9^$v{5|1)cTU~U_}gDZEKbOjlPR!8Iw4R!nexuh*)3MM"
    b"#mrlglg!PV_#1C$1-^LnI$<o}?-@(Z;qIL(J{S!3tufZ(AhKdjCCc+4o%vx_&8Q`AWYwTuXFhi(Aeb48S(^AEB~eW2B1(J)!eGR?"
    b"X1+&aC-eYn?u0Wb`Vj?L$CGq6aM@yb{KJnwe0Tiq58qydQOsP*`GAJkgJ0gAy?pV<FXS2NID8qp3Ykr(&z@uqD3-~Bc`5Lba>~iX"
    b"51n`^y8}8C+Y0^QvXCe0-dkru0hjSXvW#fAN1i<cDu4RG<_#ae{m>!*hjg@J(OcRfC)sZ#ZL;}t^x}`#Z_dUqUcY_*{vYG>=O2Cr"
    b"XhHN7^TX~%NZ`{Mb`I6Il*ie3U`Kz?IfDzmpGE6FApaqy9!FtLC~>9<jfIxgkwrzO2}(vbRtx7Dac&$xA+n#lSxb698)d^JUY-NP"
    b"35W6@bqk$%cKz|_^73b04!Ri?8=Ghrd777SJkQ7ecY^2fjF$=yIF6z2aW5+lT{aS<?{_D{(#7fnKXilCqrBVc_o2enpUKh-DBa0X"
    b"7JleYm+Py`GDf8djM{)c4}$gevSLP6m-7*tgwdSRWCPb`(SppUEM7RttH7DP4n2BX2LqSiO%X+wH8_wL$TO)uWvH(}q5DrjjfamO"
    b"f%O3`CtoVvKX@f--H}H_(k+tySO$Ddo?Q0iFj#bqHp~JxaROA^?b?x-H`<b4gt~QzS#wHU)|Ew0fl=r*It7d58O$!6+mZjTEjd+%"
    b"O(m08pjHJaWAJUdn9#T^%}RMK&N?n*Fx+Q;yd9USu)(Fe0<|hYtHf9-xJHIp_7@uN#Gou+;!$2yY?cw%aau0BhFOnBXsx#6R24SZ"
    b"R9B!@1!xt6m4a)y6E-&zHuu%mT$U;s+?MVD%1Q#OxL;YJwiW)Abic~1V~>16TO=-)aMUdVGgr)RU`D$kFXNcS-Olem=YBXNG5u%i"
    b"$KY{Q8c1N;NEdKXw3Lv$jK~xw;9H+3d<5P>w@h*+&8wGrN#UdskAc?^GLRs`osfrLN?><a8SOjOY&B#Qm2KLk$7P{CCY;pk9TC+i"
    b"DuWu+wI>Fda7f^52)44TCUL_6(-7T7T`s1a1=qA|a8($(b>&o{i!h)lNbplYsZe<Q=dz}dZ@wY*8lomw7A9~Xs6ZFT)bX%HT_+!c"
    b"H<e8LKPuIbwIe6y6rG;mM(^GZ#ILSGl*I%y&*Rux4ty^D?aDgqbmlgqZUV3lmdCikU<F;xbvi?e@&MKlc|;RUx7cUW&4yBf8SF5a"
    b"cC{1Umv^Pt2;N>Ab;WF+v5u`PxE$FX@G*r$%fdIz@w#6?1%Rs)GQ7ALK#hmPyF(&8ZPX^<@^5*?tY<jSyh-Sr2*WCHxwB5{V$A{S"
    b"hU^;MGx8GyyK!iu65ZGQ#Jq@~D@joXUQ%MGqfy?hE+Aio;x>?v0>_mq^6-o3cb>#BTEO2!Vf5}qVR|d#957){8YpPIesyRP4?s95"
    b"mJY-@#*MqIA$Ph>qR;RPxa#vHiTKGd3rA;uGEXN1pABV)@!<m&_SJw<9>401_i7a79Wr=?eNhO<x^g5R>Y;il2I65+vkr5Ywx_fY"
    b"T5u<ZB`Bvk#()!)f$mJrw)opsJ!HfLN)XC$=#WJaJ0VG&>PymXR6la>OkIFpO0Wb7cLD>9(<~BFLOOxSLwvhb*SU7~Pl|zpx-DIL"
    b"{evbBa(6;rz(}D^*j1BpPZYS5=1GnaJ+p%)l9Cwc+SzbX8h_&%Aq@QL>fV?4{V+;J$n<XV2`t3mGhOm-334C{^#;(6uJ|tJXJde="
    b"&}qUkV%O#stkVQkBTZ;ts|S;I4slYn!@%BQem5U_YT&7|&<VaStSI*M5CYBVb}r!4ImDPX({wF6&1s1J9Dqyfy~_eZr5qce3ZB(z"
    b"X6dgUeyL#I;Z!VTCu8Xj>GbY!z$&b&A-akEgbGWfC>)kZm8OilTqU%3kG@gn=ajU8Cr(mt0r8GGuWC~)Y7966|1FCf43k`*X-rFW"
    b"1#>Tr9h`QAh#!7Y1nvl*vlJqG>9n{MN&xg&kb$}B;TJ{dj!c#ba7RwlI7UHZ36yHimY%27Ooc8Eah=4SlP-;8V_t?KHK5WLdF22T"
    b"9_A$CG}N}^1o|0k{9LV|8wKhRzMc|sk?0tiT>Z*GwAVD|m^HdM_^*qD+$CEGR#ZwSFY}L!gF~`psmP9KMy_UV;EPtmL~tcdi)1HD"
    b"*xdR__enP27A}8_mjShuUkv^5lQWd2ZZHdd%rZVGCK;csNHRXKmScRdA;qXd`-_9YU?~49HUbj>@`~{lTXS*HGiwhqr&xFW8XT4E"
    b"I!q>Xfwr8&Jt-NuE;YsBVAYeZ8a`mRuGwcA4|3<IXHK@zk<f>WR9FP8A)438OLuTa0r^NOBmw_|CW;9{#`5ym5h<Lh6L6Xv_#y*&"
    b"$=u5;R<D*c48%B_h@d3ScdO`R<qR@r-AXw@giv5z;2AvC17xjKoMDfR9T$VRt`yH0UB;}k>}#lau`+RfaiEPm*h-Q9yUYz(xq(=u"
    b"XBaS1<#FCH{9SU79%PwFHBoWV%_?Url2Z;ScjUOAof(c0O!K8+_;9Ra7Fxro62LOtP-8&>3uowN?t`%y4uz*|X!?l@!*#4`kFj=!"
    b"7y}b7TF5gRQD6dr!GJJ<jn&YyV4N89<Y&U*YJ39*A5;?9bd^=bavBkb>=f*y06@POAysOb+<;dIRr%aO@048oED)_62n!;+56TA_"
    b"nhbEUtxj%W#7Y881%teogcQaIKXz5Inm+mnO0bo54#;Jx4>LdKqXw-G?Zcd&S}_QiE7~`XVkzFo6*a7`E`Vv@4XCoLks++nW95?>"
    b"Qc_JcbXi_0O9I2kx?!3b6mM+Af@9UI&&-*9eY;L-U~r6<J98bm%Zw-Uru5WIPS9Qi&V&YIKb*4e8puQ7Oc@x6LB<e_pwWM9f`N9`"
    b")E`Kds*3qz4Rd9^8|RA{fS&Sr2)=-}T^ba-WDZWmn1P03!5cr!W~3@jg@~fY)g43Ir5#>ht-@iRCA>IzSd`H4p<$8-8NYcAU0H+4"
    b"$U7foF`YP^3dOFOOJxu^mV+P2<#TIYJ+K(i93kdR?^YvR9AqggV?dg?XiLxy+k_x9NGm`@WhJfkP+C&;IOELUugADFhi^Wi8ag6G"
    b"uZ(1Mt*Oo=t~LZ2Rz5lqw3H$;no?>^d04EL%nHpdHYUpotY}5qHp(czx4A*GV4)pFj0&IY$jW?ybYC{p&a(@(^bN3`m~c5C3r~E^"
    b"W%lfwZ^+7ftCYvgzilaV9lsT3B-AjUgKkgKS<g(I)yPCjMFoBZk>DK+q>F^^3T3kSqO3{iVS@dFFe_*FP_$D6VFP<v^}0b)(}beH"
    b"WFDtRQqpD#lStOtv3z`k-rYE{H-?DC$Gr(KfWRNhI4`Fe3!jf`@1htzCvi%$tu2SCpKZ%V=>&qlv2dKXC=T&_B3#5R%3k~2pZI~F"
    b"Y%DE%P%dy(gW%fuajb*FnvnSW|2ZM?P`3dbE_d#L_YhC&V%&iW^-G~{#kUCjFul#Zi-fylm7OSfC~zHTdn2-2>7bM$h9DbI;K+%S"
    b"r78Iv8ASA*h4=mOkjL&Y@Fz$}Ix(w>EgUx^mfdutRNi&s#rMaCx3n!gfZ7*zO;)6bQ?wyF0hZgQ09C@~0U0F_D#G|CAE4q$#<oy2"
    b"Ms#6P#d4<HHG_FG7Q@WCU<rj#in^(=72WqQUXr0e^EvAc#6LQ?<N)Ia4z92%_b*|Dqidcp71Ig08y2H6x(JN{!4-33;Arf@++B>C"
    b"*0fzHRjKvx|G*n0+9=$oHgy?<(J=Dt0R?pCxXXR<*kT}-n*4;uXr(RS?T=uI8Oo2oee&JFWkEniUd9$)Y>6`~ATybpX0aVOyRuhl"
    b"d23*|OFK|8ouW?I0prWXgauZyZfFpE4%v+rmHmF;$VGOU=(huD1!S5AfgL1>(5xW+AZ0a4r&Ai+0ki|+B-9R)Et&sdQRYmp*;>KE"
    b"oG$ei;2gwDONe3%gB>g^=?N9%HUNK=v$xeb;76+-fr7LGVoP;@w5Ga0+MMBjglTC;ZzCI6KPj7U1<DGDt<L+&TIc;_(eC_Y(MtSe"
    b"&!af{b};d0ATT>vc0lY*Wq@Q!r2xs2N&!+R_MO|ovS(WW%<{a5h_*ByvF!W3M^Eg4*wV5m*0k)2P0e^>u^*p62Rff2xUvCuWNiuq"
    b"C@Uahzmy#)TS|3gO{tFT&4z$w&65Evd;0m*o=QEnn^yunD<HP?>ZvuoI<_291E5{WAKL-3CHZ5E<M7z#L~(3!P#;@&M~*F;3ClU-"
    b"qwnm1Oata5R#I;WW=UPYvt}q%?%7Tj<H%<2`RF@as=vP`?BD$%y-h65g96IpSP^)3Kx_n=63N!uoyPNd#JH8rn!vN;lnN*-HbEd+"
    b"u?Ygn>O3J3anC&Ts3rdFC3NL&+!L@T&<&laMRA*n+#J;wj@<I0Hn`YB-M_hFy8~1ddzp-F1!7}hzLmh-P;n_YsH9Lp?n}U34tifn"
    b"J;LK>Wb>8<wco-hh}(|F;&*9R!}`6<;_(Ez)1ME;K@@zrVD8)*if7tQY26pdd@A7HP}~n$XoDvSv?osj<P#rGozJwN_zN2I9U=3b"
    b"vzw76<HnUfXykrUz>QWmq$zN$bg)Nh)TjRpY;cuf?Z=sXRtk#r#bmk{XJxDkXJw<Z#2uKH2DHE$&S?<QqPucmC<}cgfy7@6Kz;F~"
    b"er%1gej3&zmb)oRqlki8kN54W45*JAiT2do{4|1?B@8@qJJzWRD@CS%_7_eRm4fXCsE7?eeJAo8rh`)|Z;RSJl~(wf{ePx3UiLY~"
    b"fUC~9*nq^*V3SEc_ZL?2lGrb%$9C@|a2LJUzlK3L@jc)2=vf8M(quTJ;c(&jVKeOK6f6wSO+;XhS>RbXEac4+N}axZp1eO7kFhmM"
    b"4DR<QK}rD@59(=()GqVa3i#4Qf`JR=-}QZeN=x_p>W~lX^s-AsFq^{{T5K;Dsd}8YVS?>6OHvW%R@^2H6FWUSA5k%=8YXl`uYH%k"
    b"r(j%pavrlq)O6F%d?#SDhKalkC?3D{t0RtyXpz>avSp_Uis7{%vk<8^P3bnFVGC;bDi#mqHcI2y4<F8zqPIecM=&Ke-Kf_Xe>6>l"
    b"^R(%9{5EQ3te&G+*DQ(m)Cm?1)A%5sVsDtppLFtPCDYpgoxMY?jT8hDi+T2?L3-%p4PU^Yci{1zdQsz~+Vr+0#m3z&lMMJ<p$Pce"
    b"qt~#1C#bdJ;7LkSfGtouqaZ(D92plkPpeJ8Rg!GlaTQeB@?0gc+lX=My-LGY6r^b{PY`Lx%`-?f?&k?wO*?vmNDE$=pwqA$CTKM6"
    b"hY2F1)(l?bE}x*%hR-Jm;h&~yi0iv~EBg@f!&yttgSg(_ye_W4b*}xH(;B%}Mz5mO%3L9?x8(AP>wmQ8%x3i3am2*+^6?8z5K0rj"
    b"$yA=}MZ@PVo_MPc1!|bq3qSPsUJlw!rOkj;5NtdiHK?=^juy!^Jb;ndsTVb!<5OhLV~Xeb%Y%ZNC)d2a6@=QDi6x<H8aL}YBbjz%"
    b"YeBK;&>D%f6<G^f+V=hKgwSD4TZ@<aEUk9zMH{O^1v(#MC*-(GxM6}#Cnz!+?Wl!G>g6@vd(*^r1V6;IX&JVY<H;#Cp5j4LO=oxn"
    b"4UQ+AQxAgtz-yCM)0)>p<_gKY@u$>Xx&i$Y--FTk0Demoe4Igy-F8fdqSy|x9nyfiW5-k->8qLqQ=@#hUel`Bc&08VBD<*-({2)M"
    b"S}$O_cbgAX|D==OPtTht(s)7@>9~!qG$RE}hBT^VY_VYB@OkVz{}*@qN_!qRPw}3S{T`%1DMrlwOp{jpT1onJG-_9@r%n(|9QX5k"
    b"I`erFFI%8!zNn@Za;PxxqFpAHJqWcG2W3R@jbXo2&Ku4+XnO3kPn(ot$`iI|cMP22Df?}Z!Gui<bl6RZ>AM$n?p*t<1p$he@U<}O"
    b"2p8(*!cUSm)B;~Nayn;>w?XU|nzXC*lF;j#Lp++H_s03x@_9UK&~y}oqUPOgN%j2f)oydkKD63Zk@JyyitBuz$IP~(<kQ%tEw@fz"
    b"Xr5HN4U@|@uftD`uYf6f)J<bQS)S3vX(jj=!3FTHsMo@pqNMo2;h&)s(-vm$5A?R>nc#!Jpe#*BG)W+wZ=wJG82txbPM8yWZ=5Ad"
    b"TX2*A)2BD>IOKmieuCQ4Jf;7_S5UXBDe|A@>4;~rn<vxo#u3`e^ItRlw(FrUEIw^=!9Y9++rk5?`y3dt0a(gt`279r=VI_^AE(jQ"
    b"D59sM(T?QqNhc4Y+ic$XeJI|ORPHdg+Cn@%uN6w@28e9}TN{u2(`m2U@;R%`UTeiLDq0QCDOj=(6HA}dB#jy-q!Zz9oHT5B8C8%I"
    b"FFvGgQG4abbjohqA}8rIn1&Wq-ol?&*DVCqb3dZ(5z{}htxXeySenxCJ)SD+HLoLK3oSweHJmo-UAKOmN%3Zbgkp-XhHW<0C;I0("
    b"9tHYZosIcNvr~#7ct)o*jvJjK1s$<c@W%g4n<mu4!;*r|?`hJ&(Lg04@|eP=snnmH+z6%GQ<a;bL_?P~{4|B2Gf$G}j~%vN>`uWC"
    b"VJgC0f)n(8uV4ETxh7hcwqSj(Y23&@W%CqaTs0Jb8l@r*<O}+rc>b+Xdhsog!aw%(&V*0nAaZcEHS9+$SWf*QsJmv7_zs!BUC-()"
    b"9}+4m+d0Zrcvd@E>CHg99i6xy54?%-oUzZ`0-Aa^O!XBp`{C^m<j(y76gOu0tey?LAmw{+G$;VF1&kc^6yZ?6Kf?;EavP8(q{P{N"
    b"<$Ntxyo?fN0mz%h&ca?lc)l|W8BctdkKMTwhBUCjWXSEH4Ar|(tiZ7y&M#dDM0eRvu?}cz2gIh3W&xWx`z~D90p_opu)rm{n3iCU"
    b"EaBJ?zJ;^F6%LOqWR&Y6vqd%wjLK^kfI@1+Ibb$A5vH}d4Hz0vt@$bT9Dyhem+tndxph$DbnNQ8&+ds9Q^yPZkXnSYX;-0)jfs_0"
    b"#|Fwj!RxXiDd;`wEUW<uk1JhJD}JDYwAhCV@rF+9fPvzVG125%aru+&A1z%A<6Dxye(8)o`6q!Dl-P0Um|EBpTR`RC4ZgouJh(M@"
    b"F=SHjRNTd*+L}@$KAVKLaD03j^oRW=vzde~*DOhF@RY_jCX*e~nd@M#f3yK&V-~q_xNlFl@P!I$Bn+JiixZfglGwtomsLb|$`R~{"
    b"4Z$zHb!oK!I?yT#)}mq)bV?I<-VDE?JRXiU)R`QR_kIj*H8^@AR)j4c^&Ka3bb4(7{tI7fct4ml^nBOvOE8e#y|;T6fwn%Zdo2ve"
    b"-3eXl^&__L+Y$=6uH(*W-wzYq#24Lv(^(bN3Xpt#4NshV5A7N}D@c1j{9FdZ-G$<p9XxzAlv`92BTw7kFHc*7<&lkZK?<O_Cl6C%"
    b"`o(0nMl#*y(aQBmVy<Fo>{{vWU1q8EKrCK(PU2YX3^`@=ckbwIKw0e1?wwID0M3uxzCGJ8mk)thQI$E&i*|&zPY6*0$|egdO<XYd"
    b"^G>658@&^tt`h|PxsSfk(i*4s^hChfvnm44@=Pu;cW^n@f(Z*?1qdqHa|*0A&+c@C9`O$QvNm8Pu~pU(ZobpAZfgN~=tO+Z5_>G}"
    b"kzA<Y_=y$k0TGn%1(uMw*jB#!wYzeP9E%gZ#35eesSh~Y?W>u_;E;~t*D#<n$6cy_`hd2>h|5tFGr%4`)jG_1+cV-5O<MAVE^H*o"
    b"5bY+$y&$#kGzNB^G)Tq?mv5toKr<8P#lg$b(YHrW6>atNcyKPW&&K7qc-W{u>Y;r76tg>GuWb3wbIc~z^5U^Yi50k#Rfq2bUu|Bl"
    b"t&X`%Xm>nj@^J`&`GQsDjwkq)#3{uKDW`vgpF?&Nz79{(kpHI>`v{9wrcS^`sgS*1L;+}O@}4GXEbuf!eh=UD6#E`6+<XO-buy1x"
    b"I-8$@eEI8ebmIdZqY%A~Xza_KmuJ})S)%|zu$$2`Oy(3ql{48)pvvOJEC!muX?#ub$e!e7m@dql{Iegu@h7Kq>V7Um|7I3+u|(kt"
    b"_|L8Y1(ySugl2%gzYg*AlDE!+nnnE(m-t2^1}F*%4^?>=irv{JSEA(aPGns3s2e!)@FueluuB<-UN<iqdsfZM;Fn_`N<Q9AL0|FV"
    b"srR@AorZX%vFFFMpa(cs-ZLh*=w0e7`q$k>cyLG#q!Hr7N$LmQFgHua*TF3G4^GIz19HluC3y1lgy5JVALf*Z$B+DUL7u0{9J=@-"
    b"d_ex-l&~pDz@JmnAh>o#;O7jFc@C*ZCQIUw7o#(94weBWplmdRYRTM5i0g!8LJ?`oz?y<Y;5sF5UZ1{vJ9<fQ^bc@w_(EN}7YBpE"
    b"F!aMu&hXj+2MyT0IOv(Thlzas{8%VLZGqe#7ch{C=dnA7K5}7{P=W*E+NU=sc;{K|2k!tu80xVTFR|}g8sX?OwLoo-z^`C^7ouzW"
    b"f;~_~0`oPZ{N`Lc@?Z)=)r*Kh7zrcCs1BeepgBC9^b>J_Atx~Ah$4&pAp{{$dgS33Sz;W5Zr}C2grCTJ@OkC%<d8(b7g)5Qz$4I1"
    b"NLLuw9#|D%EU$KzaF_J^X7PR>E1cz5-i5&uuKMb*RO1eZ%OR5k7r(uyQ0AImW$^!x#teADSpc+qNdP{LWHOVe)()~39+g`N=F$}h"
    b"rj27d&3XPsZ%84r9yUNjb~_B=?$x8dt~h`aViW9DkEpd~)Y%Nm1M8~UQB@3vo~~r5ju~m#r3_cGld~d@frDCLap!q&>}DOVNW?&o"
    b"P}4w}v|w|zIyrzT7MQv0j0PW98Z1<qdWN2*K~U9l5~m@q|C*|vh=<~|M7lu!kieh8!ijnroKQ!lg%K%BD(CDRW+5DfGnlTTZjs4e"
    b"&fRm)=|W7AYMSi}uCAhV0;UNZpom|op#WZqDV?4ktm31abA`znUG29~Pcw9utMo6i6V1uXTL-3EDmdw{`ru7*ehXO0E9nc3Cs%5G"
    b"a2y=^ssrS%(ANOUXy}PBj?Rg`eQ`i8uX?$0c-*)1c`V`nB5OaI3$p3+*H&;_e{#5wPcAbq&<>LBbK%AH?gxh-t~`VO-!kOkBdICf"
    b"Yu{O60DEg1`~NL9R$s6syQ5ro1q(~M*;YRhyY#l^&X8Hv<lL*V)@9c<OhE7cvXl1b;vgT0y-TujRBgjwW@$EZ_t!?a>z!J+rnygR"
    b"RAqyjKvR_A>TRtmYt-Z~yQ=b+-C?#)cE^V7)HJ%uOl{!KuuDnVP<`Ut!`*YX_-n;%vELxQ=jZz0IPp#XM@)Q^#wNZ=Ht~gw+&B<D"
    b"K%AW^+gH8T8k^5l6YDe8#L`S<t+7vQ8oNtFtK8)-zTra-SI^YwRkl7cTaw<fA-$5dJs~Q*sP;Uzy0?P6%=bPq2i#%)GwY?pI`_N^"
    b"sG?(@`_D3wBN7K?pR--vE4eij;nwWgyN1KHofvUsA8y^;6(ZlBzs%u+?*;xc{khx<&gK+^h^Iy#;Z?wH$T#FIX2no{5+_+7Vj7X="
    b"lL6_T>wFOAI6=`*3PBSF?uA|<RJ^4N^L(M3#$Y>=KqpHuQNhxLOadmuelwTz+sI6RV3bUbfcyu{6P&6%fk8eOG0rYrOfN;&M95`m"
    b"kYa*Ovw<Q4cAeDWdE~4HkjNd7rzism=7G|w!(GRt;FZ44L5Po0yU5ZCdE-u-8KCLLLOFlB1m+ce$r0iHEOaoB2zq=4@MA0w>Wlp|"
    b"h?ZC6Gey0^Y^NN}3z1F0Ly!>PJ5+G4Bl91!e-lO`<Q43;nM;B;SL8O3Da|wS`WWa)f|6Ew7oD(_DH4@oK?`{O!(*tIP-rqtC@|Xd"
    b">?VwNxe6g4zwWB{(scdsn&F#4l#kFmya<f}_z#wnMamO`kBWhuNJo|Hh#q}tBxzI@mXLAGSmODZX@?PfaUfFVFrk+zs$xCGSD|95"
    b"4Bo#}=$S1+qL?N#SXe=+P{p8EhN>-Z()KVtErpJat7dCuYYX;nb%jE)x|-+@$a<<8xC&zs$M9j1^}2b!h_;!Ba*f?-8+1Rd5tCMM"
    b"EGi0v`5N<V(Wa!2n8Vw+0=uf;9Z$Y&Nv@MpqaHW3z@+~;XJAd`qB@R?PS~ZstfxhJK~X4PtyfkVTAOOBYn95#`!Eox<IVvnI?M`Z"
    b"wNYznq%|CSA#f%%82jOr$to8I+3+<g$Z=R-9!F%UxYCZ5Rf<Yv)-DS0v08GKpD%_nBKQ<0;Kx)f72_{3a1TxD!-rX0>xP7B+Uh<<"
    b"9(#G|yR7DA#&)4UA4})^p_bgq!!H>pce*J0FS7cbsX!rUak7P8l$4@a%`9YqKM=D*ti4j4t>io-29<P6&4Wbr0-ji7BxrmOCCts-"
    b"cjsg{6wCj{8r+G?qUBgYj0Hp#0|i`jP$_iuA|Srx43Q-oR>A~4xCMU9lb;ELTk(z0Da>=ZV<OAp8spUQ1Hl+>;6cDY{m^4KTrmez"
    b"GgRbr2cN$q*FIBDidgqlhKS)37rpg4%+=6JCtnX0_6X1E){+;kmlM9L3f0S9b^A7A(Vee*ofa&l-s!Na@3Yc|0+<MuawaL&<4A|{"
    b"klrTBNGp#+Xh*6Rye|$Wsn~(EX4-~+gQ49c-+V(#*T}PHdLM}UiDw>1kq5k;bFq?-VzGR%;M^jXJI#go8aDHig+XIfua-0^fkG5~"
    b"7;&*?XUCd-AGK8mS*MK)y;Uuk$xKOTU8(n`@TwB6uZrXQOtjIosoD!Wth0wz5Rj9^P!d~9D%(**3M5zVE2{8tSgj0Z7t!-Mpn?XL"
    b"zZU1Rp4WU=N~U1dLit9Of)*MYmTO(5vfPkjYyioe35YMo!cqyvD*6R*4{sF)p1vskZaE6Y$xk%^#=xzc2)i2wGx_VU)#E*L{)G_o"
    b"W<Z?+TWf?;*aV{)+)7JU?LJ#E<JYUyeQB*yuFO{`O2qWbYDJLEDhmdoWXWexXtfeu510!=ci5qM$=nKQ+^X|eDA#-AYIe-6nZWRN"
    b"ar)9*>@F*pO4Stkx)#E1WlBZW$E0pcq2e4j*WzZx1p7(o8AJ~Wfcc>tq#iBD!khd>_4&A6F%<J7b2sJoRTlTC<L*%BvX^<H7~$r@"
    b"n34~@{a^Vg4dOLgRJrt(>BrYqGR+`3ifhvfSrP4p5aAbh%T*0SP#ae2>7!>xRr;zKTN+b}VR-UYq-uk83Ge{)L4!4kgD+nsg5nq4"
    b"6LizB>D^s!<?4CIV~OQS07dTe>x$&bBatM2OsX|Y&ABT?Ntlquq^N9w*taOcO^z$wF7X&NJ7kI6XNQR|ne&t9D)zF}kkS6E`@gDn"
    b"2|iT|->KcMB9`Z|dst3|7`F2^QGqrxDdTKW13dg&=o2oSo}SLCK`5@me{+fx6|}Qwmd(TX96bXmhyQXu(zQ@~rKeE5HZWoVpn~!h"
    b")_FahLR#XEQ1G)>fMnEgb{+x;kGIFwgGzK(SIQQ7#sODym5P|P@>kg9u8@!(d@Ceo44rvgKWHB2?Nqk0I8<uYtE^&02Y_rK^QmkY"
    b"Hx<YIeuXNRYX{pX+XqXYoQSkGF>-5q@1As)aE(B1h=hSC($`|auUlvMwl#Yjjh<p##`40^S?3KFH)dDY3Ua%hox^IRG{t~aHy)Rg"
    b"k)_$`<8F*l9)Ucm=qka6>5-P@q)eGPiG*5rie*Z}MkS!kyokP)TR2nG_1yx0WA(T;6wVu6h)NBFLy?&p%QOudFK9iEoqk_g|AMlc"
    b"D4;{*i61)gvVu|4_<rA?g<y<NRdT#?+t^QCbGK%Hbxo<d?S?hQ)c)(*J8H$b=AI19OE(I_D2(aN0#Pyi8pJ$xuo?^dr%=bx_)0yk"
    b"E5NDtD*pcEOHp%#lcZdT)%soULhSB96ha|fqcOaqvj5!t(uK*uPD>YgDCHR~DE4KJU6Tr`nDV2OY@<A>Zo5IB$=HdX9||XN&hlIO"
    b"Q38BO0=K^7nyGj`r3&icvZ&hZE^#gnvZVy*N0rRZ5IZ>tK6>Q|t_mC71v|z4INiF%yvC*+(}S*A3%oQ-q0LlB%ZsofUw^|0L7UT@"
    b"2OT@VD?XK$-_#E{>8zCABx61iRlHaC7)iOzif6x+_1HZHS*Tb@7VVu@tU}yBxmfaqM5mn<7sV^`t+}5?U;0tg%DBzbSH>!2aaC=-"
    b"7j{Bk-iiknN;@Uc3zJk~ps9@-$|=BsBNht8z6resgWD;R*q_a)Scj?XaWEgnDLh`1KPO}?tkGM1=4(w{oGq)#Cdmy>_0=02uf-iu"
    b"#l@lG8sRDz_-UQp6y{BssL9fJZj2nG<Rt|7!KqXCoX&5f;&VCcZQ8%Zo;6|1YFA&H9XRCp1f~6<@D$Wz5z*vbvO0qK)OY~1YrIjD"
    b"sw;L{HD9)0L{i4H9iVK;z~=~tfJ8y_y%{xmf>jy#TENPSIO-#)vh2YbeP~3vRbhz&rGZ>7g|4h=k!qr}jX6rip-NBy%~ImAuts5E"
    b")+Q$Qi>V=^UPLOS*84E<($>3`2BLUgy<ndVsLR|q)JLhs6_tKp;U2DtUUTSmv5^d6S72-xy8^@-Hj0CJCCp`g%FSXvq%3_STeV5@"
    b"D}&EZ_e>DnFGWxz>idejxYD*;#A{4Ug$HO!zOz6&MuzLuk~g$s-Djg+t2J+V?6snTY3a5$Y$n6o*|$|<GoL+NO`xW9T^73DbmDII"
    b"`k_+0T?|SWS4%{Fg9-dyPSnZ~zYU(^rV5tNYuWDmOtPP<GTpq@%|v=!-RicsTk=OPH^S+QrB*Cx;;6Rf;~}5iwRdTb`|>hrHLJBn"
    b"{vHQd%@Va~<u)!}ZnN@=tD)kY`A=J}heCz%t`#H&JhpYaN|(Lr3PSz(z|AmUW%H5Tyk^Kz_ugO@ds$zt5vfqrM}o_YiMu`Xikni2"
    b"0-u9zR$CW%{l5JN<b~Lksn#etgxm0ILP-?+*BCuw@Fyb;7W>dpRYC^Bc-9UmX+W=?Fwwg(7ci-aNmXo!6!Qe$s$@kU$>Ay(H6AXI"
    b"j%}6G{8H_#lWiU<Y%h}cjF~1+6xp1nih)tCh#zgd;HQxV7Kr$79MIlteKyn>x9w`&^<!z10RU%BCs7VJ7B%#Di_*fBZc{F^yIT}C"
    b"=glpOugimPQC)iz8;VLhaaSE5P@f*r%J3)+o7yq5hVxyt7S(?UJQQ9$nQ3WFcAygc78fzHcTm>EvzC<pe^q`jmETfliBb+$U6@>}"
    b"-*^z2`3BYcW5}YVIEC!1Vanqn1XFl?G>MkUoP|&2^N+^qWZ(arPbU)(K`>6_8DsJ(6NkeU;mql?#|RE;Jj*$|z|)C(2UmV+nSW<o"
    b"CZJ$QkY8x>LWGp(=pN#sJ~C9q)BJG5iWU<l2oJK+q$v&-jys28)fKx)I!?U!{<w2u`t|XRACA@IF&!w~mt|xT^yoK~UU;IkW=n3%"
    b"Dy|Q8&P2V=2_9pTAsmtpkKGRZ1I7&hffjVkz_#<o57S$$1ts8{qP4l6dcZ@}ypiA6)E&@Un$^Gim`P3^WZCUq=D9XyF7-nnS`ezc"
    b"2cMW9cJ&YNrqC*P$1?67_YTP;EuMV;lP)bEF6q{j=$)7;8PV~&UI?5P*Q<&(HSZ7b?5lJ^$qo2=F_li6Tl*BtY8u9R)Rryo3<jN="
    b"zbi~eCpQBfQ(C=zG`vs4{8`rb&P~@{NOJ=2H%~lh!?U;GmuiW@U35cBV+h0jte(m~$k7Oe^PxF7XD}Heh0Q^Sd*eN0d;oJXeCzf$"
    b"S5VBF)Lx&z+^Qap<K63}p^s}M;1h)O29LVr&pv>Yyt$%&P5iS?noRpYc9iZ){tp#DAUDkpL<S2W=As+ZE9jFhA%CJZJ}kkk=;uwH"
    b"T!RD*ZB~_h7>cRkt7qg}W26C!xx-wQ3_v3!i5~e|p}ejGp(^@N;KRvsCl?o?*j1>i)xjY%HE<8eui{K+n2k6-c0Q*tbX5akA_H$R"
    b"SsoI6djY`})Y)9jQP=|*&*6h_)H~G6ekLrH=g|@IrqYw)aONlTbb@_1T=;IxIGZNJTzW$vj0XfQ-#__oLwlhiW_xoXS4)G*OP+o0"
    b"LD8>;{EdQJ4_l#K4nF($ULMIOj%n?7PL&8QE;?Ux_@iV2oPEtZ<m<Ic-|c{r5{3<kp$&st>H;>P*DGqZY^Bs(=+Lq7#m8eY-Hylj"
    b"isA8C&$uw93}pF%IM}#bRCBxy{`~dFCnuLk<>2nW0W|8_A^"
)))


# Complete donor GN inventory, independent of SOURCE155_REPAIRS.
GN_FIXTURE_BYTES = gzip.decompress(
    (Path(__file__).parent / "fixtures/devtools155_gn.json.gz").read_bytes())
GN_SOURCES = json.loads(GN_FIXTURE_BYTES)["sources"]
DECLARATIONS_FIXTURE_BYTES = gzip.decompress(
    (Path(__file__).parent / "fixtures/devtools155_declarations.json.gz").read_bytes())
DECLARATIONS = json.loads(DECLARATIONS_FIXTURE_BYTES)
SCRIPTS.update(DECLARATIONS["sources"])
SCRIPTS["scripts/build/typescript/ts_type_check.gni"] = GN_SOURCES[
    "scripts/build/typescript/ts_type_check.gni"]
API = "//" + generators.DEVTOOLS + "/front_end/core/platform/api"


class Linux155GeneratorsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.src = Path(temporary.name).resolve()
        for name, text in SCRIPTS.items():
            path = self.src / generators.DEVTOOLS / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        lock = self.src / generators.LOCK
        lock.write_bytes(b"independent lock fixture")
        patch = mock.patch.object(generators, "LOCK155_SHA256",
                                  hashlib.sha256(lock.read_bytes()).hexdigest())
        patch.start()
        self.addCleanup(patch.stop)

    def test_real_reconstructed_bytes_and_every_repaired_hash(self):
        identities, changes = generators.source_plan(self.src, pins=generators.PINS155)
        self.assertEqual(len(changes), 7)
        for name, before, after, metadata in changes:
            self.assertEqual(hashlib.sha256(before).hexdigest(), identities[name]["original_sha256"])
            self.assertEqual(hashlib.sha256(after).hexdigest(), identities[name]["repaired_sha256"])
            (self.src / name).write_bytes(after)
        self.assertEqual(generators.source_plan(self.src, pins=generators.PINS155)[1], [])
        wrapper = SCRIPTS["third_party/typescript/typescript.py"].encode()
        self.assertEqual(hashlib.sha256(wrapper).hexdigest(), generators.TYPESCRIPT155_PORTABLE)

    def test_direct_tsc_commands_use_explicit_host_node_not_env(self):
        _, changes = generators.source_plan(self.src, pins=generators.PINS155)
        repaired = {name.removeprefix(generators.DEVTOOLS + "/"): after for name, _, after, _ in changes}
        variables = repaired["scripts/build/typescript/typescript_vars.gni"]
        actions = repaired["scripts/build/typescript/typescript.gni"]
        self.assertIn(b'node-linux-${host_cpu}/bin/node', variables)
        self.assertIn(b'typescript_base_inputs += [ tsc_node ]', variables)
        self.assertEqual(actions.count(b'] + tsc_command + ['), 3)
        self.assertNotIn(b'rebase_path(tsc_binary, root_build_dir)', actions)
        self.assertNotIn(b'/usr/bin/tsc', variables)
        self.assertNotIn("scripts/build/typescript/ts_library.py", generators.SOURCE155_REPAIRS)
        self.assertIn("scripts/build/typescript/ts_library.py", generators.SOURCE_REPAIRS)

    def test_complete_donor_gn_inventory_and_typecheck_repair(self):
        self.assertEqual(hashlib.sha256(GN_FIXTURE_BYTES).hexdigest(),
                         "205bf6d99e486c02f98e34d96b57d93cb6dd3416b2b9dad2ecb33e02a2c71375")
        gn_files = {name: text for name, text in GN_SOURCES.items()
                    if name.endswith((".gn", ".gni"))}
        self.assertEqual(len(gn_files), 386)
        consumers = {name: text.count("rebase_path(tsc_binary, root_build_dir)")
                     for name, text in gn_files.items() if "rebase_path(tsc_binary" in text}
        self.assertEqual(consumers, {
            "scripts/build/typescript/typescript.gni": 3,
            "scripts/build/typescript/ts_type_check.gni": 1,
        })
        self.assertEqual({name for name, text in gn_files.items() if "tsc_binary" in text},
                         set(consumers) | {"scripts/build/typescript/typescript_vars.gni"})
        for name in gn_files.keys() & SCRIPTS.keys():
            self.assertEqual(gn_files[name], SCRIPTS[name], name)
        name = "scripts/build/typescript/ts_type_check.gni"
        raw = GN_SOURCES[name].encode()
        expected = raw.replace(b'      rebase_path(tsc_binary, root_build_dir),',
                               b'    ] + tsc_command + [')
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         "bd87baff09d8e2e504fd93f8db58081491bf2c3d3eb5de3a1250ad5b7268a5fb")
        self.assertEqual(hashlib.sha256(expected).hexdigest(),
                         "cfcb7d21806175c969cee489315145c96829e25e79ff9e14bdc2e7f13aa8d208")
        _, changes = generators.source_plan(self.src, pins=generators.PINS155)
        repaired = {path.removeprefix(generators.DEVTOOLS + "/"): after
                    for path, _, after, _ in changes}
        self.assertEqual(repaired[name], expected)
        for consumer, count in consumers.items():
            self.assertEqual(repaired[consumer].count(b"] + tsc_command + ["), count)
            self.assertNotIn(b"rebase_path(tsc_binary", repaired[consumer])

    def test_vars_repaired_bytes_are_independently_derived(self):
        name = "scripts/build/typescript/typescript_vars.gni"
        raw = SCRIPTS[name].encode()
        expected = raw.replace(
            b'  tsc_binary = "/usr/bin/tsc"',
            b'  tsc_binary = devtools_location_prepend + "node_modules/typescript/lib/tsc.js"')
        expected = expected.replace(b'# Standard TypeScript library definitions', b'''if (host_os == "linux") {
  tsc_node = "//third_party/node/linux/node-linux-${host_cpu}/bin/node"
  tsc_command = [
    rebase_path(tsc_node, root_build_dir),
    rebase_path(tsc_binary, root_build_dir),
  ]
} else {
  tsc_command = [ rebase_path(tsc_binary, root_build_dir) ]
}

# Standard TypeScript library definitions''')
        expected = expected.replace(b'typescript_base_inputs = [ tsc_binary ]', b'''typescript_base_inputs = [ tsc_binary ]
if (host_os == "linux") {
  typescript_base_inputs += [ tsc_node ]
}''')
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         "aa17ae85e0be06e7c593f1e8ff947b0795565b311b8938753366836085f88572")
        self.assertEqual(hashlib.sha256(expected).hexdigest(),
                         "36650c33dd69e4cab6d51fa25b77170fe9a34a0fa2874afdc5a2b80044354a73")
        _, changes = generators.source_plan(self.src, pins=generators.PINS155)
        actual = next(after for path, _, after, _ in changes
                      if path == generators.DEVTOOLS + "/" + name)
        self.assertEqual(actual, expected)

    def _gn_fixture(self):
        # GN is opt-in locally; an explicit invalid GN path must fail, not skip.
        binary = os.environ.get("GN") or shutil.which("gn")
        if not binary:
            self.skipTest("real GN required: set GN to a GN executable or put gn on PATH")
        self.gn = str(Path(binary).resolve())
        for name, text in GN_SOURCES.items():
            path = self.src / generators.DEVTOOLS / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        _, changes = generators.source_plan(self.src, pins=generators.PINS155)
        for name, _, after, _ in changes:
            (self.src / name).write_bytes(after)
        stubs = {
            ".gn": ('buildconfig = "//BUILDCONFIG.gn"\n'
                    f'script_executable = "{Path(sys.executable).as_posix()}"\n'),
            "build/toolchain/rbe.gni": 'declare_args() { use_remoteexec = false }\n',
            generators.DEVTOOLS + "/scripts/build/ninja/vars.gni": (
                f'devtools_location = "{generators.DEVTOOLS}"\n'
                'devtools_location_prepend = "//$devtools_location/"\n'
                'build_with_chromium = false\n'
                'devtools_bundle = false\n'),
            "toolchain/BUILD.gn": '''toolchain("test") {
  tool("stamp") {
    command = "/usr/bin/touch {{output}}"
  }
  tool("copy") {
    command = "unused-copy {{source}} {{output}}"
  }
}
''',
            "BUILD.gn": f'''import("//{generators.DEVTOOLS}/scripts/build/typescript/typescript_vars.gni")
import("//{generators.DEVTOOLS}/scripts/build/typescript/typescript.gni")
proof = {{
  binary = tsc_binary
  command = tsc_command
  inputs = typescript_base_inputs
  standard_inputs = typescript_standard_lib_dts_inputs
}}
write_file("$root_build_dir/vars.json", proof, "json")
ts_library("emit") {{
  sources = [ "emit.ts" ]
}}
ts_library("check") {{
  sources = [ "check.ts" ]
  no_js_emit = true
}}
group("default") {{
  deps = [ ":emit", ":check", "{API}:api" ]
}}
''',
        }
        for name, text in stubs.items():
            path = self.src / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def _run_gn(self, *args):
        # No host node/tsc or other PATH tool may satisfy this generation test.
        environment = dict(os.environ, PATH="")
        return subprocess.run([self.gn, *args], cwd=self.src, env=environment,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=30, check=False)

    def _gn_host(self, host_os, host_cpu):
        # host_os/host_cpu are GN builtins, not undeclared --args overrides.
        (self.src / "BUILDCONFIG.gn").write_text(
            f'host_os = "{host_os}"\nhost_cpu = "{host_cpu}"\n'
            'set_default_toolchain("//toolchain:test")\n')

    def test_real_gn_linux_and_nonlinux_commands_and_action_inputs(self):
        self._gn_fixture()
        hosts = (("linux", "x64"), ("linux", "arm64"), ("mac", "x64"),
                 ("mac", "arm64"), ("win", "x64"), ("win", "arm64"))
        for host_os, host_cpu in hosts:
            for remote in (False, True):
                with self.subTest(host_os=host_os, host_cpu=host_cpu, remote=remote):
                    self._gn_host(host_os, host_cpu)
                    output = f"out/{host_os}-{host_cpu}-{remote}"
                    result = self._run_gn("gen", output, "--fail-on-unused-args",
                                          f"--args=use_remoteexec={str(remote).lower()}")
                    self.assertEqual(result.returncode, 0, result.stdout)
                    proof = json.loads((self.src / output / "vars.json").read_text())
                    if host_os == "linux":
                        binary = "//" + generators.DEVTOOLS + "/node_modules/typescript/lib/tsc.js"
                        node = f"//third_party/node/linux/node-linux-{host_cpu}/bin/node"
                        command_paths = [node, binary]
                        inputs = [binary, node]
                    else:
                        platform = ("windows-amd64" if host_os == "win" else
                                    "mac-arm64" if host_cpu == "arm64" else "mac-amd64")
                        binary = f"//third_party/typescript/{platform}/src/lib/tsc"
                        if host_os == "win":
                            binary += ".exe"
                        command_paths = inputs = [binary]
                    command = ["../../" + path[2:] for path in command_paths]
                    self.assertEqual(proof["binary"], binary)
                    self.assertEqual(proof["command"], command)
                    self.assertEqual(proof["inputs"], inputs + (proof["standard_inputs"] if remote else []))
                    description = self._run_gn("desc", output, "*", "--format=json")
                    self.assertEqual(description.returncode, 0, description.stdout)
                    targets = json.loads(description.stdout)
                    actions = {label: target for label, target in targets.items()
                               if target["type"] == "action"}
                    self.assertEqual(set(actions), {
                        "//:emit-dts", "//:emit_checked", "//:check-dts", "//:check_checked",
                        API + ":api-dts", API + ":api_checked", API + ":api_node_typecheck",
                        API + ":bundle-dts", API + ":bundle_checked",
                    })
                    for label, action in actions.items():
                        with self.subTest(action=label):
                            response = action["response_file_contents"]
                            start = response.index("--") + 1
                            self.assertEqual(response[start:start + len(command)], command)
                            self.assertTrue(response[start + len(command)].startswith("-"))
                            self.assertTrue(set(proof["inputs"]).issubset(action["inputs"]))
                            self.assertNotIn("/usr/bin/tsc", response)
                            if host_os != "linux":
                                self.assertFalse(any("/node-linux-" in path for path in action["inputs"]))

    def test_real_ninja_api_node_typecheck_launch_and_failure(self):
        node = os.environ.get("CHROMIX_TEST_NODE")
        ninja = os.environ.get("NINJA") or shutil.which("ninja")
        if not node or not ninja:
            self.skipTest("set CHROMIX_TEST_NODE and NINJA for real JS launch coverage")
        node = Path(node).resolve()
        ninja = str(Path(ninja).resolve())
        self._gn_fixture()
        template = self.src / generators.DEVTOOLS / "scripts/build/typescript/ts_type_check.gni"
        repaired = template.read_bytes()
        tsc = self.src / generators.DEVTOOLS / "node_modules/typescript/lib/tsc.js"
        tsc.parent.mkdir(parents=True, exist_ok=True)
        # Native Node runs a launch probe, not a substituted TypeScript compiler.
        probe = '''const fs = require('node:fs');
const assert = require('node:assert/strict');
assert.equal(process.argv[2], '-p');
assert.equal(process.argv.length, 4);
const config = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
assert.equal(config.compilerOptions.noEmit, true);
assert(config.compilerOptions.types.includes('node'));
assert(config.compilerOptions.types.includes('undici-types'));
fs.writeFileSync('node-launch.json', JSON.stringify({
  executable: process.execPath, script: process.argv[1], args: process.argv.slice(2)
}));
'''
        tsc.write_text(probe)
        tsc.chmod(0o644)
        for cpu in ("x64", "arm64"):
            with self.subTest(cpu=cpu):
                self._gn_host("linux", cpu)
                local_node = self.src / f"third_party/node/linux/node-linux-{cpu}/bin/node"
                local_node.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(node, local_node)
                output = f"out/ninja-{cpu}"
                result = self._run_gn("gen", output)
                self.assertEqual(result.returncode, 0, result.stdout)
                description = self._run_gn("desc", output, API + ":api_node_typecheck", "--format=json")
                self.assertEqual(description.returncode, 0, description.stdout)
                action = json.loads(description.stdout)[API + ":api_node_typecheck"]
                response = action["response_file_contents"]
                start = response.index("--") + 1
                self.assertEqual(response[start:start + 2], [
                    "../../" + local_node.relative_to(self.src).as_posix(),
                    "../../" + tsc.relative_to(self.src).as_posix(),
                ])
                self.assertIn("//" + local_node.relative_to(self.src).as_posix(), action["inputs"])
                for name in action["inputs"]:
                    path = self.src / name.removeprefix("//")
                    if not path.exists():
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text("")
                stamp = self.src / action["outputs"][0].removeprefix("//")
                target = stamp.relative_to(self.src / output).as_posix()
                def build():
                    return subprocess.run([ninja, "-C", output, "-v", target], cwd=self.src,
                                          env=dict(os.environ, PATH=""), text=True,
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          timeout=30, check=False)

                result = build()
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertTrue(stamp.exists())
                proof = json.loads((self.src / output / "node-launch.json").read_text())
                self.assertEqual(Path(proof["executable"]), local_node)
                self.assertEqual(Path(proof["script"]), tsc)
                self.assertFalse(os.access(tsc, os.X_OK))
                stamp.unlink()
                tsc.write_text(probe + "process.exit(23);\n")
                result = build()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(stamp.exists())
                tsc.write_text(probe)

                # The same non-executable JS fails with the unmodified donor template.
                template.write_text(GN_SOURCES["scripts/build/typescript/ts_type_check.gni"])
                result = self._run_gn("gen", output)
                self.assertEqual(result.returncode, 0, result.stdout)
                result = build()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("PermissionError: [Errno 13]", result.stdout)
                self.assertFalse(stamp.exists())
                template.write_bytes(repaired)
                result = self._run_gn("gen", output)
                self.assertEqual(result.returncode, 0, result.stdout)
                result = build()
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertTrue(stamp.exists())

    def test_real_gn_reproduces_nonempty_list_regression(self):
        self._gn_fixture()
        path = self.src / generators.DEVTOOLS / "scripts/build/typescript/typescript_vars.gni"
        fixed = path.read_text()
        broken = fixed.replace('if (host_os == "linux") {\n  tsc_node =',
                               'tsc_command = [ rebase_path(tsc_binary, root_build_dir) ]\n'
                               'if (host_os == "linux") {\n  tsc_node =', 1)
        broken = broken.replace('''  tsc_command = [
    rebase_path(tsc_node, root_build_dir),
    rebase_path(tsc_binary, root_build_dir),
  ]
} else {
  tsc_command = [ rebase_path(tsc_binary, root_build_dir) ]''',
                                 '  tsc_command = [ rebase_path(tsc_node, root_build_dir) ] + tsc_command', 1)
        self.assertEqual(hashlib.sha256(broken.encode()).hexdigest(),
                         "f4f451e28e6ee82ba690438fd270f3c66d130443d4f61195fa36116479baaeea")
        path.write_text(broken)
        for cpu in ("x64", "arm64"):
            with self.subTest(cpu=cpu):
                self._gn_host("linux", cpu)
                result = self._run_gn("gen", f"out/broken-{cpu}")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("typescript_vars.gni:23:3: Replacing nonempty list", result.stdout)

    def test_version_profiles_never_replace154_or_infer155_from_files(self):
        old = generators.generator_profile(generators.PINS)
        new = generators.generator_profile(generators.PINS155)
        self.assertEqual(old["version"], "0.25.1")
        self.assertEqual(new["version"], "0.28.2")
        self.assertNotEqual(old["module"], new["module"])
        self.assertNotEqual(old["binaries"], new["binaries"])
        self.assertEqual(generators.PINS[0], "154.0.8037.97")
        with self.assertRaisesRegex(ValueError, "package lock"):
            generators.source_plan(self.src)
        for pins in ((), list(generators.PINS155), ("155.0.8059.40", *generators.PINS155[1:]),
                     (generators.PINS155[0], generators.PINS[1], generators.PINS155[2])):
            with self.subTest(pins=pins), self.assertRaisesRegex(ValueError, "pins"):
                generators.generator_profile(pins)

    def test_15579_uses_the_exact_reviewed_repairs_and_rejects_mixed_pins(self):
        self.assertEqual(generators.generator_profile(generators.PINS15579),
                         generators.generator_profile(generators.PINS155))
        self.assertEqual(generators.source_plan(self.src, pins=generators.PINS15579),
                         generators.source_plan(self.src, pins=generators.PINS155))
        for pins in ((generators.PINS15579[0], *generators.PINS155[1:]),
                     (generators.PINS155[0], *generators.PINS15579[1:])):
            with self.assertRaisesRegex(ValueError, "pins"):
                generators.generator_profile(pins)

    def test_unknown_or_cross_version_sources_fail_before_execution(self):
        for name in generators.SOURCE155_REPAIRS:
            path = self.src / generators.DEVTOOLS / name
            original = path.read_bytes()
            path.write_bytes(original + b"unknown")
            with mock.patch.object(generators.subprocess, "run") as run, self.assertRaises(ValueError):
                generators.prepare_esbuild(self.src, host_arch="x64", repair=True, pins=generators.PINS155)
            run.assert_not_called()
            path.write_bytes(original)

    def test_inspection_never_executes_and_missing_packages_block_finish(self):
        with mock.patch.object(generators.subprocess, "run") as run:
            state = generators.prepare_esbuild(self.src, host_arch="arm64", pins=generators.PINS155)
            self.assertEqual(state["version"], "0.28.2")
            self.assertTrue(state["install_needed"])
            with self.assertRaisesRegex(ValueError, "installed before finish"):
                generators.prepare_esbuild(self.src, host_arch="arm64", repair=True,
                                           pins=generators.PINS155)
        run.assert_not_called()

    def test_pinned_probe_version_and_repair_dispatch_are_version_specific(self):
        profile = generators.generator_profile(generators.PINS155)
        module = {"path": generators.ESBUILD_MODULE, "files": profile["module"]}
        binary = {"path": generators.ESBUILD_BINARY, "sha256": profile["binaries"]["x64"],
                  "host_arch": "x64"}
        def publish(src, name, before, after, **kwargs):
            self.assertEqual((src / name).read_bytes(), before)
            (src / name).write_bytes(after)
        def probe(command, **kwargs):
            if command[1] == "--version":
                return generators.subprocess.CompletedProcess(command, 0, "0.28.2\n")
            self.assertIn('0.28.2', command[2])
            self.assertIn('node-linux-x64/bin/node', command[0])
            return generators.subprocess.CompletedProcess(command, 0, "")
        with mock.patch.object(generators, "module_identity", return_value=module) as module_check, \
                mock.patch.object(generators, "binary_identity", return_value=binary) as binary_check, \
                mock.patch.object(generators.subprocess, "run", side_effect=probe) as run, \
                mock.patch.object(generators, "_atomic_replace", side_effect=publish) as writer:
            state = generators.prepare_esbuild(self.src, host_arch="x64", repair=True,
                                               pins=generators.PINS155)
            self.assertFalse(state["repair_needed"])
            self.assertEqual(writer.call_count, 7)
            self.assertEqual(run.call_count, 2)
            for call in (*module_check.call_args_list, *binary_check.call_args_list):
                self.assertEqual(call.kwargs["pins"], generators.PINS155)
            generators.prepare_esbuild(self.src, host_arch="x64", repair=True,
                                       pins=generators.PINS155)
            self.assertEqual(writer.call_count, 7)

    def test_unknown_archive_and_host_reject_before_install(self):
        archive = self.src / "unknown.tgz"
        archive.write_bytes(b"unknown archive")
        for pins in (generators.PINS, generators.PINS155):
            with self.assertRaisesRegex(ValueError, "SHA256"):
                generators.archive_files(archive, "module", pins=pins)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            generators.prepare_esbuild(self.src, host_arch="ia32", pins=generators.PINS155)


if __name__ == "__main__":
    unittest.main()
