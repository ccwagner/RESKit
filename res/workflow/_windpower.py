from ._util import *
from res.windpower import *

##################################################################
## Make a typical simulator function
def simulateLocations(wsSource, landcover, adjustMethod, gwa, roughness, loss, wscorr_a, wscorr_b, lctype, minCF, verbose, 
                      extractor, powerCurve, pcKey, gid, locationID, globalStart, locations, capacity, conv_stdBase, 
                      conv_stdScale, hubHeight, rotordiam, cutout, pickleable):
    if verbose: 
        startTime = dt.now()
        globalStart = globalStart
        print(" %s: Starting at +%.2fs"%(str(gid), (startTime-globalStart).total_seconds()))
    
    # prepare wind speed loader
    locations = LocationSet(locations)

    if locations.count == 0 : 
        if verbose: print( " %s: No locations found"%(str(gid)))
        return None

    if pickleable: locations.makePickleable()

    # read and spatially adjust windspeeds
    if adjustMethod == "lra":
        ws = wsSource.get("windspeed", locations, forceDataFrame=True)
        ws = windutil.adjustLraToGwa( ws, locations, longRunAverage=MerraSource.LONG_RUN_AVERAGE_50M_SOURCE, gwa=gwa)

    elif adjustMethod == "near" or adjustMethod == "bilinear" or adjustMethod == "cubic":
        ws = wsSource.get("windspeed", locations, interpolation=adjustMethod, forceDataFrame=True)

    elif adjustMethod is None:
        ws = wsSource.get("windspeed", locations, forceDataFrame=True)

    else: raise ResError("adjustMethod not recognized")
    
    # Look for bad values
    badVals = np.isnan(ws)
    if badVals.any().any():
        print("%d locations have invalid wind speed values:"%badVals.any().sum())
        sel = badVals.any()
        for loc in locations[sel]: print("  ", loc)

    # apply wind speed corrections to account (somewhat) for local effects not captured on the MERRA context
    if not (wscorr_a is None and wscorr_b is None):
        factors = (1-wscorr_a)*(1-np.exp(-wscorr_b*ws))+wscorr_a # dampens lower wind speeds
        ws = factors*ws
        factors = None

    # Get roughnesses from Land Cover
    if roughness is None and not lctype is None:
        lcVals = gk.raster.extractValues(landcover, locations).data
        roughnesses = windutil.roughnessFromLandCover(lcVals, lctype)

        if np.isnan(roughnesses).any():
            raise RuntimeError("%d locations are outside the given landcover file"%np.isnan(roughnesses).sum())

    elif not roughness is None and lctype is None:
        roughnesses = roughness
    else:
        raise ResError("roughness and lctype are both given or are both None")

    # Project WS to hub height
    ws = windutil.projectByLogLaw(ws, measuredHeight=50, targetHeight=hubHeight, roughness=roughnesses)

    # do simulations
    if not isinstance(powerCurve, dict):
        capacityGeneration = simulateTurbine(ws, powerCurve=powerCurve, loss=loss)
    else:
        capacityGeneration = pd.DataFrame(-1*np.ones(ws.shape), index=ws.index, columns=ws.columns)
        for key in np.unique(pcKey):
            tmp = simulateTurbine(ws.iloc[:,pcKey==key], powerCurve[key], loss=loss)
            capacityGeneration.update( tmp )

        if (capacityGeneration.values<0).any(): raise RuntimeError("Some placements were not evaluated")

    capacityFactor = capacityGeneration.mean(axis=0)
    production = capacityGeneration*capacity

    if verbose:
        endTime = dt.now()
        simSecs = (endTime - startTime).total_seconds()
        globalSecs = (endTime - globalStart).total_seconds()
        print(" %s: Finished %d turbines +%.2fs (%.2f turbines/sec)"%(str(gid), len(locations), globalSecs, len(locations)/simSecs))
    
    # Apply Capacity Factor Filter
    if minCF > 0:
        sel = capacityFactor >= minCF
        capacityFactor = capacityFactor[sel]
        production = production.ix[:,sel]

    # Done!
    if extractor.method == "batch":
        outputVars = OrderedDict()
        outputVars["lctype"] = lctype
        outputVars["minCF"] = minCF
        outputVars["pcKey"] = pcKey
        outputVars["adjustMethod"] = adjustMethod
        outputVars["roughness"] = roughness
        outputVars["loss"] = loss
        outputVars["wscorr_a"] = wscorr_a
        outputVars["wscorr_b"] = wscorr_b
        outputVars["conv_stdBase"] = conv_stdBase
        outputVars["conv_stdScale"] = conv_stdScale
        outputVars["hubHeight"] = hubHeight
        outputVars["capacity"] = capacity
        outputVars["rotordiam"] = rotordiam
        outputVars["cutout"] = cutout
        outputVars["locationID"] = locationID
        
        result = raw_finalizer(production, capacityFactor)

        try:
            outputPath = extractor.outputPath%gid
        except:
            if extractor.outputPath.format(0) == extractor.outputPath.format(2):
                raise ResError("output is not integer-formatable. Be sure there is a single %d or a {}")
            outputPath = extractor.outputPath.format(gid)

        raw_output(outputPath, result, outputVars)
    
    output = extractor.finalize(production, capacityFactor)
    return output

##################################################################
## Distributed Wind production from a Merra wind source
def WindWorkflowTemplate(placements, merra, hubHeight, powerCurve, capacity, rotordiam, cutout, extract, output, 
                         roughness, landcover, adjustMethod, gwa, lctype, conv_stdBase, conv_stdScale, loss, wscorr_a, wscorr_b, 
                         minCF, jobs, batchSize, verbose, padding=2):
    startTime = dt.now()
    if verbose:
        print("Starting at: %s"%str(startTime))

    if jobs==1: # use only a single process
        cpus = 1
        pool = None
        useMulti = False
    elif jobs > 1: # uses multiple processes (equal to jobs)
        cpus = jobs
        useMulti = True
    else: # uses multiple processes (equal to the number of available processors - jobs)
        cpus = cpu_count()-jobs
        if cpus <=0: raise ResError("Bad jobs count")
        useMulti = True
    
    extractor = Extractor(extract, outputPath=output)

    ### Determine the total extent which will be simulated (also make sure the placements input is okay)
    if verbose: print("Arranging placements at +%.2fs"%((dt.now()-startTime).total_seconds()))
    if isinstance(placements, str): # placements is a path to a point-type shapefile
        placements = gk.vector.extractAsDataFrame(placements, outputSRS='latlon')
        placements["lat"] = placements.geom.apply(lambda x: x.GetY())
        placements["lon"] = placements.geom.apply(lambda x: x.GetX())
    
    if isinstance(placements, pd.DataFrame):
        if "powerCurve" in placements.columns and powerCurve is None: powerCurve = placements.powerCurve.values
        if "turbine" in placements.columns and powerCurve is None: powerCurve = placements.turbine.values
        if "hubHeight" in placements.columns and hubHeight is None: hubHeight = placements.hubHeight.values
        if "capacity" in placements.columns and capacity is None: capacity = placements.capacity.values
        if "rotordiam" in placements.columns and rotordiam is None: rotordiam = placements.rotordiam.values
        if "cutout" in placements.columns and cutout is None: cutout = placements.cutout.values

        try:
            placements = placements[["lon","lat"]].values
        except:
            placements = placements["geom"].values

    placements = LocationSet(placements)

    lonMin, latMin, lonMax, latMax = placements.getBounds()
    latMin = latMin -padding
    latMax = latMax +padding
    lonMin = lonMin -padding
    lonMax = lonMax +padding

    hubHeight = None if hubHeight is None else np.array(hubHeight)
    capacity = None if capacity is None else np.array(capacity)
    rotordiam = None if rotordiam is None else np.array(rotordiam)
    cutout = None if cutout is None else np.array(cutout)

    if verbose: print("Pre-loading windspeeds at +%.2fs"%((dt.now()-startTime).total_seconds()))
    totalExtent = gk.Extent((lonMin,latMin,lonMax,latMax,), srs=LATLONSRS)
    
    # Setup manager if needed
    doload=True
    if useMulti:
        manager = WSManager()
        manager.start()
        wsSource = manager.MerraSource(path=merra, bounds=Bounds(*totalExtent.pad(2).xyXY))
    else:
        if isinstance(merra,str):
            wsSource = MerraSource(path=merra, bounds=totalExtent.pad(2))
        elif isinstance(merra, MerraSource):
            wsSource = merra
            doload=False
    if doload:
        wsSource.loadWindSpeed(height=50)

    ### Convolute turbine
    if verbose: print("Convolving power curves at +%.2fs"%( (dt.now()-startTime).total_seconds()) )
    
    pcKey = None
    if powerCurve is None: # no turbine given, so a synthetic turbine will need to be constructed
        if capacity is None and rotordiam is None:
            raise RuntimeError("powerCurve, capacity, and rotordiam cannot all be None")

        # Compute specific capacity
        #  - Round to the nearest 10 to save memory and time for convolution
        specificCapacity = np.array(capacity*1000/(np.pi*rotordiam**2/4))
        specificCapacity = np.round(specificCapacity, -1).astype(int)

        if specificCapacity.size == 1:
            powerCurve = SyntheticPowerCurve( specificCapacity=specificCapacity, cutout=cutout)
            pcKey = "%d:%d"%(specificCapacity,25 if cutout is None else cutout)
        else:
            powerCurve = dict()
            pcKey = []
            if isinstance(cutout, int) or isinstance(cutout, float) or cutout is None: 
                cutout = [cutout]*specificCapacity.size
            for sp,co in zip(specificCapacity,cutout):
                key = "%d:%d"%(sp,25 if co is None else co)
                pcKey.append( key )
                if not key in powerCurve.keys():
                    powerCurve[key] = SyntheticPowerCurve( sp, co)

    elif isinstance(powerCurve, str):
        pcKey = powerCurve
        capacity = TurbineLibrary.ix[powerCurve].Capacity
        powerCurve = TurbineLibrary.ix[powerCurve].PowerCurve

    else: # powerCurve is either a (ws,power) list or is a list of turbine names
        if isinstance(powerCurve[0],str): # assume entire list is a list of names
            pcKey = powerCurve
            powerCurve = dict()
            capacity = []

            for name in pcKey:
                capacity.append(TurbineLibrary.ix[name].Capacity)
                if not name in powerCurve.keys():
                    powerCurve[name] = TurbineLibrary.ix[name].PowerCurve
        
        else: # powerCurve is a single power curve definition
            if capacity is None:
                raise RuntimeError("capacity cannot be None when giving a user-defined power curve")
            tmp = np.array(powerCurve)
            powerCurve = PowerCurve(tmp[:,0], tmp[:,1])
            pcKey = None

        turbine = None # remove turbine so it doesn't show up in output
    pcKey = pcKey if pcKey is None or isinstance(pcKey, str) else np.array(pcKey)

    if isinstance(powerCurve, dict):
        if verbose: 
            print("   Convolving %d power curves..."%(len(powerCurve)))

        if useMulti:
            pool = Pool(cpus)
            res = []
        
            for k,v in powerCurve.items():
                kwargs = dict(stdScaling=conv_stdScale, stdBase=conv_stdBase, powerCurve=v)
                res.append((k,pool.apply_async(convolutePowerCurveByGuassian, (), kwargs)))

            for k,r in res:
                powerCurve[k] = r.get()
            
            pool.close()
            pool.join()
            pool = None
        else:
            for k,v in powerCurve.items():
                powerCurve[k] = convolutePowerCurveByGuassian(stdScaling=conv_stdScale, stdBase=conv_stdBase, powerCurve=v)
    else:
        powerCurve = convolutePowerCurveByGuassian(stdScaling=conv_stdScale, stdBase=conv_stdBase, powerCurve=powerCurve )

    ### Do simulations
    # Check inputs
    if hubHeight is None:
        raise RuntimeError("hubHeight has not been provided")

    ### initialize simulations
    if verbose: print("Initializing simulations at +%.2fs"%((dt.now()-startTime).total_seconds()))
    if useMulti: pool = Pool(cpus)
    simGroups = []
    I = np.arange(placements.count)
    if batchSize is None and cpus==1: # do everything in one big batch
        simGroups.append( I )
    elif cpus>1 and (batchSize is None or placements.count < batchSize): # Split evenly to all cpus
        for simPlacements in np.array_split( I, cpus):
            simGroups.append( simPlacements )
    else: # split the area in to equal size groups, and simulate one group at a time
        for simPlacements in np.array_split(I, max(1,placements.count//(batchSize/cpus))):
            simGroups.append( simPlacements )

    if verbose: 
        print("Simulating %d groups at +%.2fs"%(len(simGroups), (dt.now()-startTime).total_seconds() ))

    results = []
    if useMulti: placements.makePickleable()
    for i,sel in enumerate(simGroups):
        # Construct arguments for each submission
        inputs = {}
        inputs["wsSource"]=wsSource
        inputs["landcover"]=landcover
        inputs["gwa"]=gwa
        inputs["adjustMethod"]=adjustMethod
        inputs["roughness"]=roughness
        inputs["loss"]=loss
        inputs["wscorr_a"]=wscorr_a
        inputs["wscorr_b"]=wscorr_b
        inputs["conv_stdBase"]=conv_stdBase
        inputs["conv_stdScale"]=conv_stdScale
        inputs["lctype"]=lctype
        inputs["minCF"]=minCF
        inputs["verbose"]=verbose
        inputs["extractor"]=extractor
        inputs["powerCurve"] = powerCurve
        inputs["pcKey"] = pcKey if (pcKey is None or isinstance(pcKey, str)) else pcKey[sel]
        inputs["gid"]=i
        inputs["locationID"]=sel
        inputs["globalStart"]=startTime

        def add(val,name):
            if isinstance(val, list): val = np.array(val)
            if isinstance(val , np.ndarray) and val.size>1: inputs[name] = val[sel]
            else: inputs[name] = val

        add(placements[:], "locations")
        add(capacity, "capacity")
        add(hubHeight, "hubHeight")
        add(rotordiam, "rotordiam")
        add(cutout, "cutout")
        
        if useMulti:
            inputs["pickleable"]=True
            results.append( pool.apply_async( simulateLocations, (), inputs ))
        else:    
            inputs["pickleable"]=False
            results.append( PoollikeResult(simulateLocations(**inputs)) )

    # Read each result as it becomes available
    finalResult = None
    for r in results:
        tmp = r.get()
        if tmp is None: continue
        finalResult = extractor.combine(finalResult, r.get())
       
    if useMulti:
        # Do some cleanup
        pool.close()
        pool.join()

    if verbose:
        endTime = dt.now()
        totalSecs = (endTime - startTime).total_seconds()
        print("Finished simulating %d turbines (%d surviving) at +%.2fs (%.2f turbines/sec)"%(placements.count, finalResult.c, totalSecs, placements.count/totalSecs))

    ### Give the results
    if not output is None and not extractor.skipFinalOutput:
        if verbose:
            endTime = dt.now()
            totalSecs = (endTime - startTime).total_seconds()
            print("Writing output at +%.2fs"%totalSecs)

        outputVars = OrderedDict()
        outputVars["lctype"] = lctype
        outputVars["minCF"] = minCF
        outputVars["pcKey"] = pcKey
        outputVars["adjustMethod"] = adjustMethod
        outputVars["roughness"] = roughness
        outputVars["loss"] = loss
        outputVars["wscorr_a"] = wscorr_a
        outputVars["wscorr_b"] = wscorr_b
        outputVars["conv_stdBase"] = conv_stdBase
        outputVars["conv_stdScale"] = conv_stdScale
        outputVars["hubHeight"] = hubHeight
        outputVars["capacity"] = capacity
        outputVars["rotordiam"] = rotordiam
        outputVars["cutout"] = cutout
        
        extractor.output(output, finalResult, outputVars)

    #### Formulate result
    if verbose:
        endTime = dt.now()
        totalSecs = (endTime - startTime).total_seconds()
        print("Done at +%.2fs!"%totalSecs)

    if extract == "batch":  return

    outputResult = finalResult.o
    outputResult.name = extractor.title
    outputResult.TurbineCount = finalResult.c
    return outputResult
    
def WindOnshoreWorkflow(placements, merra, landcover, gwa, hubHeight=None, powerCurve=None, capacity=None, rotordiam=None, cutout=None, lctype="clc", extract="totalProduction", output=None, minCF=0, jobs=1, batchSize=None, verbose=True, **kwargs):
    """
    Apply the wind simulation method developed by Severin Ryberg, Dilara Caglayan, and Sabrina Schmitt. This method 
    works as follows for a given simulation point:
        1. The nearest time-series in the provided MERRA climate data is extracted
            * reads windspeeds at 50 meters
        2. The time series is adjusted so that the long-run-average (all MERRA data at this point) matches the value
           given by the Global Wind Atlas at the simulation point
        3. A roughness factor is assumed from the land cover and is used to project the wind speeds to the indicated
           hub height
        4. Low windspeeds are depressed slightly, ending around 10 m/s
        5. The wind speeds are fed through the power-curve of the indicated turbine
            * The power curve has been convoluted to incorporate a stochastic spread of windspeeds
        6. An additional 4% loss is applied
    
    Notes:
        * hubHeight must always be given, either as an argument or contained within the placements object (see below)
        * When giving a user-defined power curve, the capacity must also be given 
        * When powerCurve isn't given, capacity, rotordiam and cutout are used to generate a synthetic power curve 
          using res.windpower.SyntheticPowerCurve. In this case, rotordiam and capacity must be given, but cutout can
          be left as None (implying the default of 25 m/s)
        * Be careful about writing raw production data to csv files. It takes long and output is big big big. I 
          suggest using a netCDF4 (.nc) file instead

    Inputs:
        placements: 
            [ (lon,lat), ] : a list of (lon,lat) coordinates to simulate
            str : A path to a point-type shapefile indicating the turbines to simulate
            DataFrame : A datafrom containing per-turbine characteristics, must include a 'lon' and 'lat' column
            * When plaements is given as a shapefile path or a DataFrame, the following can also be defined as 
              attributes/columns for each turbine (but are not necessary):
              [turbine, powerCurve, capacity, rotordiam, hubHeight, cutout]

        merra - str : A path to the MERRA data which will be used for the simulation
            * MUST have the fields 'U50M' and 'V50M'

        landcover - str : The path to the land cover source

        gwa - str : The path to the global wind atlas source

        powerCurve: The turbine to simulate
            str : An indicator from the TurbineLibrary (res.windpower.TurbineLibrary)
            [(float, float), ...] : An explicit performance curve given as (windspeed, capacity-factor-output) pairs
            * Giving this will overload the turbine/powerCurve definition from the placements shapefile/DataFrame

        hubHeight - float : The hub height to simulate at
            * Giving this will overload the hubHeight definition from the placements shapefile/DataFrame

        capacity - float : The turbine capacity in kW
            * Giving this will overload the capacity definition from the placements shapefile/DataFrame

        rotordiam - float : The turbine rotor diameter in m
            * Giving this will overload the rotordiam definition from the placements shapefile/DataFrame
            * Using this is only useful when generating a synthetic power curve
        
        cutout - float : The turbine cutout windspeed in m/s
            * Giving this will overload the cutout definition from the placements shapefile/DataFrame
            * Using this is only useful when generating a synthetic power curve
        
        lctype - str: The land cover type to use
            * Options are "clc", "globCover", and "modis"

        extract - str: Determines the extraction method and the form of the returned information
            * Options are:
                "raw" - returns the timeseries production for each location
                "capacityFactor" - returns only the resulting capacity factor for each location
                "averageProduction" - returns the average time series of all locations
                "batch" - returns nothing, but the full production data is written independently for each batch

        minCF - float : The minimum capacity factor to accept
            * Must be between 0..1

        jobs - int : The number of parallel jobs

        batchSize - int : The number of placements to simulate across all concurrent jobs
            * Use this to tune performance to your specific machine

        verbose - bool : False means silent, True means noisy

        output - str : The path of the output file to create
            * File type options are ".shp", ".csv", and ".nc"
            * When using the "batch" extract option, output must be able to handle an integer when formatting.
                - ex: output="somepath\outputData_%02d.nc"
    """
    return WindWorkflowTemplate(placements=placements, merra=merra, hubHeight=hubHeight, powerCurve=powerCurve, 
                                capacity=capacity, rotordiam=rotordiam, cutout=cutout, extract=extract, 
                                output=output, landcover=landcover, gwa=gwa, lctype=lctype, minCF=minCF, jobs=jobs, 
                                batchSize=batchSize, verbose=verbose,
                                roughness=None,
                                adjustMethod="lra",
                                conv_stdBase=0.6,
                                conv_stdScale=0.1,
                                loss=0.04,
                                wscorr_a=0.3,
                                wscorr_b=0.2,)
                         
                                                  
def WindOffshoreWorkflow(placements, merra, adjustMethod="bilinear", loss=0.03, conv_stdBase=0.2, conv_stdScale=0.05, wscorr_a=0.45, wscorr_b=0.2,hubHeight=None, powerCurve=None, capacity=None, rotordiam=None, cutout=None, roughness=0.0002, extract="averageProduction", output=None, minCF=0, jobs=1, batchSize=None, verbose=True, **kwargs):
    return WindWorkflowTemplate(placements=placements, merra=merra, hubHeight=hubHeight, powerCurve=powerCurve, capacity=capacity, 
                                rotordiam=rotordiam, cutout=cutout, extract=extract, output=output, landcover=None, gwa=None, 
                                roughness=roughness, minCF=minCF, jobs=jobs,batchSize=batchSize, verbose=verbose, lctype=None,
                                adjustMethod=adjustMethod,conv_stdBase=conv_stdBase,conv_stdScale=conv_stdScale,loss=loss, wscorr_a=wscorr_a, wscorr_b=wscorr_b,)